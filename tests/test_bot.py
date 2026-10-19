"""Модули бота: номера, сопоставление, фото, распознавание, сервис против поддельного InvenTree."""

import asyncio
import io
import json
from types import SimpleNamespace

import pytest
import qrcode
from aiohttp import web

from bot import images, service
from bot.handlers.add import parse_edit
from bot.inventree import Api, Catalog
from bot.matching import best_match
from bot.numbering import Numbering
from bot.recognize import (ChainRecognizer, ClaudeRecognizer, LocalRecognizer, RecognitionError,
                           UNKNOWN_CATEGORY)
from bot.storage import Storage
from bot.ui import card_kb, card_text
from tests.fake_inventree import TOKEN, FakeInvenTree, seed

CATS = ["DJ-оборудование / Проигрыватели", "DJ-оборудование / Микшеры"]
REC = {"manufacturer": "Pioneer DJ", "model": "XDJ-1000MK2", "serial": "VJXK012345",
       "category": "DJ-оборудование / Проигрыватели", "overview_photo": 1, "confidence": "high", "notes": ""}


def run(coro):
    return asyncio.run(coro)


# --- номера и сопоставление


def test_numbering():
    n = Numbering()
    assert n.format(42) == "P2PTD0042" and n.format(12345) == "P2PTD12345"
    assert n.parse(" p2ptd0042 ") == 42 and n.parse("P2PTD-00042") == 42 and n.parse("P2P-0042") is None
    assert n.normalize("p2ptd-00042") == "P2PTD0042" and n.normalize("SN-1") == "SN-1"
    assert n.highest([None, "SN123", "P2PTD-00007", "P2PTD0012"]) == 12
    assert n.highest([]) == 0


PARTS = [{"pk": 1, "name": "Pioneer DJ XDJ-1000MK2"}, {"pk": 2, "name": "Shure SM58S"}]
MPNS = [{"part": 1, "MPN": "XDJ-1000MK2"}]


def test_matching():
    assert best_match("Pioneer", "XDJ1000 MK2", PARTS, MPNS).part["pk"] == 1
    assert best_match("Shure", "SM58S", PARTS, MPNS).score == 100
    assert best_match("Shure", "SM58-S", PARTS, MPNS).part["pk"] == 2
    assert best_match("Yamaha", "MG12XU", PARTS, MPNS) is None
    assert best_match("Yamaha", "", PARTS, MPNS) is None


# --- фото


def png(img):
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def test_images():
    raw = png(qrcode.make(json.dumps({"stockitem": 17})).convert("RGB"))
    jpeg, codes = images.analyze(raw)
    assert jpeg[:2] == b"\xff\xd8" and [images.stockitem_from_qr(c) for c in codes] == [17]
    assert images.stockitem_from_qr("SN-123") is None
    label = images.label_png(17, "P2PTD0042", "Pioneer DJ XDJ-1000MK2", "Пешка / Диджейка")
    assert [images.stockitem_from_qr(c) for c in images.read_barcodes(label)] == [17]


# --- распознавание


class FakeAnthropic:
    def __init__(self, payload, stop_reason="end_turn"):
        self.payload, self.stop_reason, self.kwargs = payload, stop_reason, None
        self.beta = SimpleNamespace(messages=self)

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(stop_reason=self.stop_reason,
                               content=[SimpleNamespace(type="text", text=json.dumps(self.payload))])


def test_claude_request():
    client = FakeAnthropic(REC)
    rec = run(ClaudeRecognizer("claude-opus-5-5", "low", client).recognize([b"a", b"b"], CATS, ["X"], ["VJ"]))
    assert rec.name == "Pioneer DJ XDJ-1000MK2" and rec.engine == "claude" and rec.overview_photo == 1
    cfg = client.kwargs["output_config"]
    assert cfg["effort"] == "low" and CATS[0] in cfg["format"]["schema"]["properties"]["category"]["enum"]
    assert client.kwargs["fallbacks"] == "default"
    assert [b["type"] for b in client.kwargs["messages"][0]["content"]] == ["image", "image", "text"]


def test_claude_refusal_and_cleanup():
    with pytest.raises(RecognitionError):
        run(ClaudeRecognizer("m", client=FakeAnthropic(REC, "refusal")).recognize([b"a"], CATS))
    bad = {**REC, "overview_photo": 5, "category": "Выдумано", "confidence": "?"}
    rec = run(ClaudeRecognizer("m", client=FakeAnthropic(bad)).recognize([b"a"], CATS))
    assert rec.overview_photo == -1 and rec.category == UNKNOWN_CATEGORY and rec.confidence == "low"


def local_llm_app(reply, seen):
    async def models(request):
        return web.json_response({"data": [{"id": "qwen2.5vl"}, {"id": "gpt-oss-20b"}]})

    async def completions(request):
        seen.append(await request.json())
        if reply is None:
            return web.Response(status=503)
        if isinstance(reply, dict):  # полный choice: content + finish_reason
            return web.json_response({"choices": [reply]})
        return web.json_response({"choices": [{"message": {"content": reply}}]})
    app = web.Application()
    app.router.add_post("/v1/chat/completions", completions)
    app.router.add_get("/v1/models", models)
    return app


async def with_local_llm(reply, fn, **kw):
    seen = []
    runner = web.AppRunner(local_llm_app(reply, seen))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    local = LocalRecognizer(f"http://127.0.0.1:{port}/v1", kw.get("model", "qwen2.5vl"), timeout=5,
                            think=kw.get("think"))
    try:
        return await fn(local), seen
    finally:
        await local.close()
        await runner.cleanup()


def test_local_llm_openai_compatible():
    reply = "```json\n" + json.dumps(REC) + "\n```"  # локальные модели любят оборачивать JSON
    rec, seen = run(with_local_llm(reply, lambda local: local.recognize([b"a"], CATS)))
    assert rec.engine == "local" and rec.model == "XDJ-1000MK2"
    body = seen[0]
    assert body["model"] == "qwen2.5vl" and body["response_format"]["type"] == "json_schema"
    assert body["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_local_llm_think_and_check():
    async def go(local):
        await local.recognize([b"a"], CATS)
        return await local.check()
    problem, seen = run(with_local_llm(json.dumps(REC), go, think=False))
    assert problem is None and seen[0]["chat_template_kwargs"] == {"enable_thinking": False}
    problem, seen = run(with_local_llm(json.dumps(REC), go, think="low", model="nope"))
    assert seen[0]["reasoning_effort"] == "low" and "модели nope нет" in problem


@pytest.mark.parametrize("reply", [
    "<think>На шильдике XDJ-1000MK2, серийник VJXK012345…</think>\n" + json.dumps(REC),
    "Вот результат:\n```json\n" + json.dumps(REC, ensure_ascii=False) + "\n```\nПроверьте серийник.",
    "Ответ: " + json.dumps(REC) + " — готово {не json}",
])
def test_local_llm_tolerates_thinking_and_prose(reply):
    rec, _ = run(with_local_llm(reply, lambda local: local.recognize([b"a"], CATS)))
    assert rec.model == "XDJ-1000MK2" and rec.serial == "VJXK012345"


def test_local_llm_thinking_off_by_default_and_length_error():
    reply = {"message": {"content": "", "reasoning_content": "думаю…"}, "finish_reason": "length"}

    async def go(local):
        with pytest.raises(RecognitionError, match="лимит токенов"):
            await local.recognize([b"a"], CATS)
    _, seen = run(with_local_llm(reply, go))
    assert seen[0]["chat_template_kwargs"] == {"enable_thinking": False} and seen[0]["max_tokens"] == 8192


def test_local_llm_garbage_is_logged(caplog):
    async def go(local):
        with pytest.raises(RecognitionError, match="не JSON"):
            await local.recognize([b"a"], CATS)
    run(with_local_llm("не могу прочитать шильдик", go))
    assert "не могу прочитать шильдик" in caplog.text


def test_chain_falls_back_to_claude():
    async def go(local):
        return await ChainRecognizer([local, ClaudeRecognizer("m", client=FakeAnthropic(REC))]).recognize([b"a"], CATS)
    rec, _ = run(with_local_llm(None, go))  # локальный сервер отвечает 503
    assert rec.engine == "claude"


# --- сервис против поддельного InvenTree


@pytest.fixture
def server():
    s = FakeInvenTree()
    seed(s)
    yield s
    s.close()


class Static:
    """Распознаватель с заранее заданным ответом."""

    def __init__(self, **kw):
        self.values = {**REC, **kw}

    async def recognize(self, photos, categories, known_models=(), barcodes=()):
        from bot.recognize import Recognition
        return Recognition(**self.values, engine="static")


def photo(name="p.jpg", raw=b"jpeg", barcodes=()):
    return service.Photo(name, raw, raw, list(barcodes))


async def session(server):
    api = Api(server.url)
    return api, api.client(TOKEN), Catalog(), Numbering()


def test_existing_model_from_rider(server):
    async def go():
        api, it, catalog, numbering = await session(server)
        photos = [photo("p1.jpg", b"jpeg1", ["VJXK012345"]), photo("p2.jpg", b"jpeg2")]
        draft, _ = await service.recognize(it, catalog, Static(), photos, 11, "Пешка / Диджейка")
        assert draft.part_pk == 1 and draft.serial_confirmed and draft.rider_qty == 2
        assert "✓ совпал со штрихкодом" in card_text(draft)

        result = await service.save(it, catalog, numbering, draft, photos, "anna")
        again, _ = await service.recognize(it, catalog, Static(), photos, 11, "Пешка / Диджейка")
        await api.close()
        return result, again

    result, again = run(go())
    assert result.number == "P2PTD0008" and not result.warnings and not result.created_part
    item = server.get("stock", result.stock_pk)
    assert (item["serial"], item["batch"], item["status"]) == ("P2PTD0008", "VJXK012345", 10)
    assert server.get("stock", 1)["quantity"] == 1
    assert server.get("part", 1)["trackable"] is True
    assert len(server.db["attachment"]) == 2 and server.part_images[1] == b"jpeg2"
    assert again.duplicate == "P2PTD0008" and not again.can_save
    assert "Сохранить" not in str(card_kb(again))


def test_new_model_and_catalog_cache(server):
    async def go():
        api, it, catalog, numbering = await session(server)
        rec = Static(model="DJM-750MK2", serial="", category=UNKNOWN_CATEGORY, overview_photo=-1)
        draft, _ = await service.recognize(it, catalog, rec, [photo()], 11, "Пешка / Диджейка")
        assert draft.part_pk is None and not draft.can_save
        with pytest.raises(ValueError):
            await service.save(it, catalog, numbering, draft, [], "anna")
        draft.category_pk = 3
        result = await service.save(it, catalog, numbering, draft, [], "anna")
        await catalog.load(it)  # после создания детали кэш перечитывается
        await api.close()
        return result, catalog

    result, catalog = run(go())
    part = server.get("part", server.get("stock", result.stock_pk)["part"])
    assert result.created_part and part["name"] == "Pioneer DJ DJM-750MK2" and part["category"] == 3
    assert any(m["MPN"] == "DJM-750MK2" for m in server.db["mpn"])
    assert any(p["name"] == "Pioneer DJ DJM-750MK2" for p in catalog.parts)


def test_catalog_loads_right_after_boot(server, monkeypatch):
    """time.monotonic() отсчитывается от старта системы и на свежей машине меньше TTL."""
    import bot.inventree
    monkeypatch.setattr(bot.inventree.time, "monotonic", lambda: 10.0)

    async def go():
        api, it, catalog, _ = await session(server)
        await catalog.load(it)
        await api.close()
        return catalog
    assert run(go()).parts


def test_catalog_loaded_once(server):
    async def go():
        api, it, catalog, _ = await session(server)
        await catalog.load(it)
        before = server.requests
        await catalog.load(it)
        await api.close()
        return server.requests - before
    assert run(go()) == 0


def test_numbers_continue_without_refetch_and_resync_on_conflict(server):
    async def go():
        api, it, catalog, numbering = await session(server)
        draft, _ = await service.recognize(it, catalog, Static(serial="A1"), [photo()], 11, "x")
        first = await service.save(it, catalog, numbering, draft, [], "anna")
        # кто-то занял следующий номер в обход бота
        server.db["stock"].append({"pk": 99, "part": 1, "location": 10, "quantity": 1,
                                   "serial": "P2PTD0009", "batch": None})
        draft, _ = await service.recognize(it, catalog, Static(serial="A2"), [photo()], 11, "x")
        second = await service.save(it, catalog, numbering, draft, [], "anna")
        await api.close()
        return first.number, second.number
    assert run(go()) == ("P2PTD0008", "P2PTD0010")


def test_lookup(server):
    async def go():
        api, it, _, numbering = await session(server)
        out = (await service.lookup(it, numbering, "p2ptd0007"), await service.lookup(it, numbering, "P2PTD-00007"),
               await service.lookup(it, numbering, "X1"), await service.lookup(it, numbering, "nothing"))
        await api.close()
        return out
    new_format, old_format, by_batch, nothing = run(go())
    # в базе номер ещё в старом формате — находится при любом написании
    assert new_format[0]["pk"] == old_format[0]["pk"] == by_batch[0]["pk"] == 2 and nothing == []


def test_parse_edit():
    assert parse_edit("Производитель: Yamaha\nМодель: MG12XU\nS/N: ABC 1\nмусор") == {
        "manufacturer": "Yamaha", "model": "MG12XU", "serial": "ABC 1"}


def test_event_journal(tmp_path):
    store = Storage(tmp_path / "bot.sqlite3")
    store.log_event(1, "anna", "saved", number="P2PTD-00001", model="Shure SM58S")
    store.log_event(1, "anna", "lookup", query="X1", found=0)
    events = store.recent_events()
    assert [e.action for e in events] == ["lookup", "saved"] and events[1].details["number"] == "P2PTD-00001"


# --- подключение к olegovic-llm


OLEGOVIC_TOML = """
[llm]
backend = "openai"
base_url = "http://127.0.0.1:8080"
[llm.models]
smart = "qwen3.6-35b-a3b"
vision = "{vision}"
[llm.think]
"qwen3.6-35b-a3b" = false
"""


def test_olegovic_config(tmp_path):
    from bot import olegovic
    path = tmp_path / "config.toml"
    path.write_text(OLEGOVIC_TOML.format(vision=""), encoding="utf-8")
    found = olegovic.load(path)
    assert (found.url, found.model, found.think) == ("http://127.0.0.1:8080/v1", "qwen3.6-35b-a3b", False)
    path.write_text(OLEGOVIC_TOML.format(vision="gemma-4-31b"), encoding="utf-8")
    assert olegovic.load(path).model == "gemma-4-31b"
    path.write_text("[general]\nlanguage = 'ru'\n", encoding="utf-8")  # всё по умолчанию, как в olegovic
    assert olegovic.load(path).url == "http://127.0.0.1:11434/v1"


def test_config_with_olegovic(tmp_path, monkeypatch):
    from bot import config
    path = tmp_path / "config.toml"
    path.write_text(OLEGOVIC_TOML.format(vision=""), encoding="utf-8")
    monkeypatch.setattr(config, "load_env", lambda: None)
    for key in ("LOCAL_LLM_URL", "LOCAL_LLM_MODEL", "LOCAL_LLM_THINK", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.setenv("INVENTREE_URL", "https://inv.example/")
    monkeypatch.setenv("OLEGOVIC_CONFIG", str(path))
    monkeypatch.setenv("BOT_RECOGNIZER", "olegovic")
    cfg = config.load()
    assert cfg.recognizers == ("local",) and cfg.local_llm_model == "qwen3.6-35b-a3b"
    assert cfg.local_llm_url == "http://127.0.0.1:8080/v1" and cfg.local_llm_think is False

    monkeypatch.setenv("BOT_RECOGNIZER", "olegovic,claude")  # с запасным Claude нужен ключ
    with pytest.raises(SystemExit):
        config.load()
    monkeypatch.setenv("LOCAL_LLM_THINK", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    cfg = config.load()
    assert cfg.recognizers == ("local", "claude") and cfg.local_llm_think is True


def test_label_text_fits():
    from PIL import Image, ImageDraw
    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    for number in ("P2PTD0008", "P2PTD-00008", "P2PTD123456"):
        assert draw.textlength(number, font=images._fit(draw, number, 358, 64)) <= 358
    lines = images._wrap(draw, "Behringer XENYX 1202SFX аналоговый пульт", images._font(32), 358)
    assert all(draw.textlength(line, font=images._font(32)) <= 358 for line in lines) and len(lines) >= 2


# --- правка позиций


def test_item_edits(server):
    async def go():
        api, it, _, _ = await session(server)
        moved = await service.move(it, 2, 11, "anna")
        status = await service.set_status(it, 2, 55)
        await service.add_note(it, 2, "царапина на корпусе", "anna")
        noted = await service.add_note(it, 2, "проверено", "anna")
        with pytest.raises(ValueError, match="уже у позиции"):
            server.db["stock"].append({"pk": 50, "part": 1, "location": 10, "quantity": 1,
                                       "serial": "P2PTD0050", "batch": "DUP", "notes": ""})
            await service.set_batch(it, 2, "DUP")
        batch = await service.set_batch(it, 2, "NEW-SN")
        await service.add_photo(it, 2, service.Photo("p.jpg", b"jpeg", b"jpeg", []), "anna")
        await api.close()
        return moved, status, noted, batch

    moved, status, noted, batch = run(go())
    assert moved["location"] == 11 and server.transfers[-1]["location"] == 11
    assert status["status"] == 55 and batch["batch"] == "NEW-SN"
    assert "царапина на корпусе" in noted["notes"] and noted["notes"].rstrip().endswith("anna: проверено")
    assert server.db["attachment"][-1]["model_id"] == "2"


def test_renumber_plan():
    from renumber_serials import plan
    items = [{"pk": 1, "serial": "P2PTD-00008"}, {"pk": 2, "serial": "P2PTD0009"},
             {"pk": 3, "serial": "P2PTD-00009"}, {"pk": 4, "serial": "SN1"}, {"pk": 5, "serial": None}]
    changes, skipped = plan(items, Numbering())
    assert changes == [(1, "P2PTD-00008", "P2PTD0008")] and skipped == [(3, "P2PTD-00009", "P2PTD0009")]


def test_renumber_script(server, monkeypatch, capsys):
    import renumber_serials
    monkeypatch.setattr("sys.argv", ["renumber_serials.py", "--url", server.url, "--token", TOKEN])
    renumber_serials.main()
    assert server.get("stock", 2)["serial"] == "P2PTD0007" and "Переименовано: 1" in capsys.readouterr().out


# --- этикетки InvenTree


def test_qr_formats():
    assert images.stockitem_from_qr("INV-SI42") == 42      # короткий формат InvenTree (по умолчанию)
    assert images.stockitem_from_qr("SI7") == 7
    assert images.stockitem_from_qr('{"stockitem": 5}') == 5  # JSON, как раньше рисовал бот
    assert images.stockitem_from_qr("INV-SL3") is None       # это место, а не позиция
    assert images.stockitem_from_qr("P2PTD0008") is None


def test_install_label_template(server, monkeypatch, capsys):
    import install_label_template
    argv = ["install_label_template.py", "--url", server.url, "--token", TOKEN]
    monkeypatch.setattr("sys.argv", argv)
    install_label_template.main()
    created = server.db["label"][0]
    assert (created["name"], created["model_type"], created["width"], created["height"]) == \
        ("P2PTD 62x29", "stockitem", "62", "29")
    assert b"{% qrcode qr_data %}" in created["template"]
    monkeypatch.setattr("sys.argv", argv + ["--width", "50", "--height", "25"])
    install_label_template.main()  # повторный запуск обновляет тот же шаблон
    assert len(server.db["label"]) == 1 and server.db["label"][0]["width"] == "50"
    assert "обновить шаблон" in capsys.readouterr().out


def test_label_from_inventree_and_fallback(server):
    from bot.labels import Labels

    async def go():
        api, it, _, _ = await session(server)
        fallback = await Labels("P2PTD 62x29", poll=0).make(it, 2, "P2PTD0007", "Pioneer", "Пешка")
        server.add("label", {"name": "P2PTD 62x29", "model_type": "stockitem", "enabled": "true"})
        rendered = await Labels("P2PTD 62x29", poll=0).make(it, 2, "P2PTD0007", "Pioneer", "Пешка")
        own = await Labels("", poll=0).make(it, 2, "P2PTD0007", "Pioneer", "Пешка")
        await api.close()
        return fallback, rendered, own

    fallback, rendered, own = run(go())
    assert fallback[0] == "P2PTD0007.png" and fallback[1][:4] == b"\x89PNG"  # шаблона нет — рисует бот
    assert rendered == ("P2PTD0007.pdf", b"%PDF-1.7 fake label") and server.printed[-1]["items"] == [2]
    assert own[0].endswith(".png")  # BOT_LABEL_TEMPLATE пустой — только своя этикетка
