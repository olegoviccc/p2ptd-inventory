"""Проверка находок моделью, очередь «На проверку», управление из бота."""

import asyncio
import fcntl
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from bot.handlers import common, routers
from bot.inventree import Api, Catalog
from bot.labels import Labels
from bot.llm import LocalLLM
from bot.middlewares import Context
from bot.numbering import Numbering
from bot.photos import PhotoBuffer
from bot.storage import Storage
from scraper import finders
from scraper.engine import Busy, Scraper
from scraper.sources import Sources
from scraper.state import State
from scraper.verify import MATCH, MISMATCH, UNSURE, Verdict, Verifier
from tests.fake_inventree import TOKEN
from tests.test_flow import USER, FakeTelegram, StaticRecognizer, message, press
from tests.test_scraper import FakeSearch, scraper_cfg, with_site

PART = {"manufacturer": "Yamaha", "model": "MG12XU"}
YAMAHA = Sources({"yamaha": ["127.0.0.1"]}, [], [])


# --- проверка моделью


async def with_llm(replies, fn):
    """Поддельная модель: отвечает по очереди из replies (None — ошибка 503)."""
    seen = []

    async def completions(request):
        seen.append(await request.json())
        reply = replies.pop(0) if replies else None
        if reply is None:
            return web.Response(status=503)
        return web.json_response({"choices": [{"message": {"content": json.dumps(reply)}}]})
    app = web.Application()
    app.router.add_post("/v1/chat/completions", completions)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    llm = LocalLLM(f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1", "qwen", timeout=5)
    try:
        return await fn(Verifier(llm)), seen
    finally:
        await llm.close()
        await runner.cleanup()


def test_verifier_verdicts_and_fallback():
    async def go(v):
        return (await v.photo(b"jpeg", PART), await v.manual("MG12XU Owner's Manual", PART),
                await v.photo(b"jpeg", PART), await v.manual("MG12XU", PART))
    replies = [{"verdict": "match", "reason": "на фото MG12XU"}, {"verdict": "mismatch", "reason": "это даташит"}]
    (photo, manual, photo_down, manual_down), seen = asyncio.run(with_llm(replies, go))
    assert (photo.verdict, manual.verdict) == (MATCH, MISMATCH) and manual.reason == "это даташит"
    assert photo_down.verdict == UNSURE and manual_down.verdict == MATCH  # модель недоступна
    assert seen[0]["messages"][0]["content"][1]["type"] == "image_url"
    assert "MG12XU Owner's Manual" in seen[1]["messages"][0]["content"][0]["text"]


class ScriptedVerifier:
    """Решения по очереди: для фото и мануалов отдельно."""

    def __init__(self, photos=(), manuals=()):
        self.photos, self.manuals = list(photos), list(manuals)

    async def photo(self, jpeg, part):
        return Verdict(self.photos.pop(0) if self.photos else MATCH, "фото: решение модели")

    async def manual(self, text, part):
        return Verdict(self.manuals.pop(0) if self.manuals else MATCH, "мануал: решение модели")


def test_finders_follow_verdicts():
    async def go(base, w):
        search = FakeSearch(base)
        # первый подходящий PDF модель отвергла → других нет → None
        rejected = await finders.find_manual(PART, search, w, YAMAHA, verifier=ScriptedVerifier(manuals=[MISMATCH]))
        unsure = await finders.find_manual(PART, search, w, YAMAHA, verifier=ScriptedVerifier(manuals=[UNSURE]))
        photo = await finders.find_photo(PART, search, w, YAMAHA, verifier=ScriptedVerifier(photos=[UNSURE]))
        skip = await finders.find_manual(PART, search, w, YAMAHA, rejected=frozenset({f"{base}/manuals/MG12XU_om.pdf"}))
        return rejected, unsure, photo, skip
    rejected, unsure, photo, skip = asyncio.run(with_site(go))
    assert rejected is None
    assert unsure.status == "review" and unsure.reason == "мануал: решение модели"
    assert photo.status == "review" and photo.data[:2] == b"\xff\xd8"
    assert skip is None  # отклонённая человеком ссылка больше не предлагается


# --- очередь и движок


def make_scraper(catalog, tmp_path, base, w, verifier):
    cfg = scraper_cfg(tmp_path, verify=True)
    cfg.inventree_token = TOKEN
    api = Api(catalog.url)
    s = Scraper(cfg, api.client(TOKEN), State(tmp_path / "scraper.sqlite3"), YAMAHA, w, FakeSearch(base),
                llm=None, store=Storage(tmp_path / "bot.sqlite3"))
    s.verifier = lambda: verifier
    return s, api


def test_review_queue_accept_and_reject(catalog, tmp_path):
    async def go(base, w):
        s, api = make_scraper(catalog, tmp_path, base, w, ScriptedVerifier(photos=[UNSURE], manuals=[MATCH, UNSURE]))  # Pioneer, Yamaha
        report = await s.run("тест", only=None)
        queued = s.state.reviews()
        manual = next(r for r in queued if r.kind == "manual")
        photo = next(r for r in queued if r.kind == "photo")
        await s.accept(manual.id, "anna")
        s.reject(photo.id)
        rejected = s.state.rejected(2, "photo")
        await api.close()
        return report, queued, rejected, s

    report, queued, rejected, s = asyncio.run(with_site(go))
    assert sorted(report.review) == [("Yamaha MG12XU", "мануал"), ("Yamaha MG12XU", "фото")]
    assert all(r.path.exists() is False for r in queued)  # файлы убраны после решения
    assert any(int(a["model_id"]) == 2 for a in catalog.db["attachment"])  # принятый мануал приложен
    assert 2 not in catalog.part_images and rejected  # отклонённое фото не поставлено, ссылка запомнена
    assert not s.state.due(2, "photo", 30) and s.state.reviews() == []
    events = [(e.username, e.action) for e in s.store.recent_events()]
    assert ("anna", "manual_added") in events


def test_settings_and_lock(catalog, tmp_path):
    async def go(base, w):
        s, api = make_scraper(catalog, tmp_path, base, w, ScriptedVerifier())
        s.state.set_setting("photos", False)
        s.state.set_setting("limit", 1)
        only_manuals = await s.run("тест")
        lock = open(s.state.dir / "scraper.lock", "w")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # «ночной прогон» в другом процессе
        with pytest.raises(Busy, match="ночной"):
            await s.run("тест")
        lock.close()
        await api.close()
        return only_manuals, s.state.last_run()

    only_manuals, last = asyncio.run(with_site(go))
    assert only_manuals.checked == 1 and not only_manuals.photos and len(only_manuals.manuals) == 1
    assert last["trigger"] == "тест" and last["manuals"] == 1


# --- управление из бота


async def bot_scenario(catalog, tmp_path, base, w):
    # фото Yamaha: «не уверена» и в проверке без записи, и в настоящем прогоне
    s, service_api = make_scraper(catalog, tmp_path, base, w, ScriptedVerifier(photos=[UNSURE, UNSURE]))
    tg = FakeTelegram()
    bot = Bot("123:TEST", session=tg)
    api = Api(catalog.url)
    store = s.store
    store.save(USER.id, "anna", TOKEN)
    cfg = SimpleNamespace(allowed_ids=frozenset(), admin_ids=frozenset({USER.id}), inventree_url=catalog.url)
    dp = Dispatcher(storage=MemoryStorage(), cfg=cfg, api=api, store=store, catalog=Catalog(),
                    numbering=Numbering(), recognizer=StaticRecognizer(), photos=PhotoBuffer(),
                    labels=Labels("", poll=0), scraper=s)
    dp.message.outer_middleware(Context())
    dp.callback_query.outer_middleware(Context())
    dp.errors.register(common.on_error)
    for r in routers():
        if r.parent_router:
            r.parent_router.sub_routers.remove(r)
            r._parent_router = None
    dp.include_routers(*routers())
    send = lambda update: dp.feed_update(bot, update)  # noqa: E731

    async def settle():
        while s.running:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)

    try:
        await send(message("🗂 Каталог"))
        assert "Без мануала: <b>2</b>, без фото: <b>2</b>" in tg.texts()[-1]

        await send(press(tg.last_buttons()["🧪 Без записи (3)"]))
        await settle()
        assert "проверка, без записи" in tg.texts()[-1] and not catalog.db["attachment"]

        await send(press(tg.last_buttons()["🗂 К каталогу"]))
        await send(press(tg.last_buttons()["▶️ Запустить (20)"]))
        await settle()
        report = tg.texts()[-1]
        assert "📘 Мануалы" in report and "📥 На проверку: 1" in report

        await send(press(tg.last_buttons()["🗂 К каталогу"]))
        await send(press(tg.last_buttons()["📥 На проверку (1)"]))
        assert tg.sent[-1][0] == "SendPhoto" and "Yamaha MG12XU" in tg.texts()[-1]
        await send(press(tg.last_buttons()["✅ Принять"]))
        assert 2 in catalog.part_images and "Очередь пуста" in tg.texts()[-1]

        await send(press(tg.last_buttons()["⬅️ К каталогу"]))
        await send(press(tg.last_buttons()["✅ 🌙 Ночью"]))
        assert s.settings()["nightly"] is False

        # карточка позиции: мануал приложен ночным поиском → бот присылает файл
        await send(message("X1"))
        await send(press(tg.last_buttons()["📘 Мануал"]))
        assert tg.sent[-1][0] == "SendDocument"
    finally:
        await api.close()
        await service_api.close()


def test_catalog_in_bot(catalog, tmp_path):
    asyncio.run(with_site(lambda base, w: bot_scenario(catalog, tmp_path, base, w)))
