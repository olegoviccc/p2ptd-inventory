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
    n = Numbering("P2PTD-", 5)
    assert n.format(42) == "P2PTD-00042"
    assert n.parse(" p2ptd-00042 ") == 42 and n.parse("P2P-00042") is None
    assert n.highest([None, "SN123", "P2PTD-00007", "P2PTD-00012"]) == 12
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
    label = images.label_png(17, "P2PTD-00042", "Pioneer DJ XDJ-1000MK2", "Пешка / Диджейка")
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
    async def completions(request):
        seen.append(await request.json())
        if reply is None:
            return web.Response(status=503)
        return web.json_response({"choices": [{"message": {"content": reply}}]})
    app = web.Application()
    app.router.add_post("/v1/chat/completions", completions)
    return app


async def with_local_llm(reply, fn):
    seen = []
    runner = web.AppRunner(local_llm_app(reply, seen))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    local = LocalRecognizer(f"http://127.0.0.1:{port}/v1", "qwen2.5vl", timeout=5)
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
    return api, api.client(TOKEN), Catalog(), Numbering("P2PTD-", 5)


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
    assert result.number == "P2PTD-00008" and not result.warnings and not result.created_part
    item = server.get("stock", result.stock_pk)
    assert (item["serial"], item["batch"], item["status"]) == ("P2PTD-00008", "VJXK012345", 10)
    assert server.get("stock", 1)["quantity"] == 1
    assert server.get("part", 1)["trackable"] is True
    assert len(server.db["attachment"]) == 2 and server.part_images[1] == b"jpeg2"
    assert again.duplicate == "P2PTD-00008" and not again.can_save
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
                                   "serial": "P2PTD-00009", "batch": None})
        draft, _ = await service.recognize(it, catalog, Static(serial="A2"), [photo()], 11, "x")
        second = await service.save(it, catalog, numbering, draft, [], "anna")
        await api.close()
        return first.number, second.number
    assert run(go()) == ("P2PTD-00008", "P2PTD-00010")


def test_lookup(server):
    async def go():
        api, it, _, numbering = await session(server)
        out = (await service.lookup(it, numbering, "p2ptd-00007"), await service.lookup(it, numbering, "X1"),
               await service.lookup(it, numbering, "nothing"))
        await api.close()
        return out
    by_number, by_batch, nothing = run(go())
    assert by_number[0]["pk"] == 2 and by_batch[0]["pk"] == 2 and nothing == []


def test_parse_edit():
    assert parse_edit("Производитель: Yamaha\nМодель: MG12XU\nS/N: ABC 1\nмусор") == {
        "manufacturer": "Yamaha", "model": "MG12XU", "serial": "ABC 1"}


def test_event_journal(tmp_path):
    store = Storage(tmp_path / "bot.sqlite3")
    store.log_event(1, "anna", "saved", number="P2PTD-00001", model="Shure SM58S")
    store.log_event(1, "anna", "lookup", query="X1", found=0)
    events = store.recent_events()
    assert [e.action for e in events] == ["lookup", "saved"] and events[1].details["number"] == "P2PTD-00001"
