"""Сквозной сценарий через настоящие обработчики aiogram.

Telegram подменён сессией, которая записывает ответы бота; InvenTree — поддельный
сервер; распознавание — заглушка. Проверяется путь «войти → добавить → фото →
распознать → сохранить → найти», кнопки меню и журнал.
"""

import asyncio
import io
import json
from datetime import datetime
from itertools import count
from types import SimpleNamespace

import qrcode
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, File, Message, PhotoSize, Update, User

from bot import logs, ui
from bot.handlers import common, routers
from bot.inventree import Api, Catalog
from bot.middlewares import Context
from bot.numbering import Numbering
from bot.photos import PhotoBuffer
from bot.recognize import Recognition
from bot.storage import Storage
from tests.fake_inventree import FakeInvenTree, seed

USER = User(id=500, is_bot=False, first_name="Anna")
ADMIN = 500
CHAT = Chat(id=500, type="private")
ids = count(1)


class FakeTelegram(BaseSession):
    """Принимает вызовы Bot API и запоминает, что бот отправил."""

    def __init__(self):
        super().__init__()
        self.sent = []   # (метод, текст, клавиатура)
        self.files = {}  # file_id → bytes

    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
        text = getattr(method, "text", None) or getattr(method, "caption", None)
        self.sent.append((name, text, getattr(method, "reply_markup", None)))
        if name == "GetFile":
            return File(file_id=method.file_id, file_unique_id=method.file_id, file_path=method.file_id)
        if name in ("SendMessage", "SendDocument", "EditMessageText"):
            return Message(message_id=next(ids), date=datetime.now(), chat=CHAT, text=text)
        return True

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        yield self.files[url.rsplit("/", 1)[1]]

    async def close(self):
        pass

    def texts(self):
        return [t for _, t, _ in self.sent if t]

    def last_buttons(self):
        markup = next(m for _, _, m in reversed(self.sent) if m is not None and hasattr(m, "inline_keyboard"))
        return {b.text: b.callback_data for row in markup.inline_keyboard for b in row}


class StaticRecognizer:
    async def recognize(self, photos, categories, known_models=(), barcodes=()):
        return Recognition(manufacturer="Pioneer DJ", model="XDJ-1000MK2", serial="VJXK012345",
                           category="DJ-оборудование / Проигрыватели", overview_photo=0,
                           confidence="high", notes="", engine="static")


def message(text=None, photo=None):
    return Update(update_id=next(ids), message=Message(
        message_id=next(ids), date=datetime.now(), chat=CHAT, from_user=USER, text=text,
        photo=[PhotoSize(file_id=photo, file_unique_id=photo, width=100, height=100)] if photo else None))


def press(data):
    return Update(update_id=next(ids), callback_query=CallbackQuery(
        id=str(next(ids)), from_user=USER, chat_instance="ci", data=data,
        message=Message(message_id=next(ids), date=datetime.now(), chat=CHAT, text="…")))


def qr_png(payload):
    out = io.BytesIO()
    qrcode.make(payload).convert("RGB").save(out, "PNG")
    return out.getvalue()


async def scenario(tmp_path):
    logs.setup(tmp_path, "INFO")
    server = FakeInvenTree()
    seed(server)
    tg = FakeTelegram()
    bot = Bot("123:TEST", session=tg)
    api = Api(server.url)
    store = Storage(tmp_path / "bot.sqlite3")
    cfg = SimpleNamespace(allowed_ids=frozenset(), admin_ids=frozenset({ADMIN}), inventree_url=server.url)
    dp = Dispatcher(storage=MemoryStorage(), cfg=cfg, api=api, store=store, catalog=Catalog(),
                    numbering=Numbering("P2PTD-", 5), recognizer=StaticRecognizer(), photos=PhotoBuffer())
    dp.message.outer_middleware(Context())
    dp.callback_query.outer_middleware(Context())
    dp.errors.register(common.on_error)
    dp.include_routers(*routers())
    send = lambda update: dp.feed_update(bot, update)  # noqa: E731

    try:
        # без входа добавить нельзя
        await send(message(ui.ADD))
        assert "Сначала войдите" in tg.texts()[-1]

        # вход
        await send(message(ui.LOGIN))
        await send(message("anna"))
        await send(message("secret"))
        assert any(name == "DeleteMessage" for name, _, _ in tg.sent), "сообщение с паролем удалено"
        assert "вы вошли как <b>anna</b>" in tg.texts()[-1]

        # место
        await send(message(ui.ADD))
        await send(press(tg.last_buttons()["Пешка"]))
        await send(press(tg.last_buttons()["Диджейка"]))
        assert "Пешка / Диджейка" in tg.texts()[-1]

        # два фото: на первом штрихкод с серийником
        tg.files["f1"] = qr_png("VJXK012345")
        tg.files["f2"] = qr_png("just a photo")
        await asyncio.gather(send(message(photo="f1")), send(message(photo="f2")))
        await send(press(tg.last_buttons()["🔍 Распознать (2 фото)"]))
        card = tg.texts()[-1]
        assert "Pioneer DJ XDJ-1000MK2" in card and "✓ совпал со штрихкодом" in card and "2 шт." in card

        # сохранить
        await send(press(ui.SAVE))
        assert any("✅ <b>P2PTD-00008</b>" in t for t in tg.texts())
        assert server.get("stock", 1)["quantity"] == 1 and len(server.db["attachment"]) == 2

        # место запомнено: следующий /add сразу просит фото
        await send(message(ui.ADD))
        assert "Пришлите 1–3 фото" in tg.texts()[-1]

        # поиск кнопкой и просто текстом
        await send(message(ui.FIND))
        await send(message("VJXK012345"))
        assert "P2PTD-00008" in tg.texts()[-1]

        # поиск по фото QR-этикетки вне сценария
        await send(message("/cancel"))
        tg.files["f3"] = qr_png(json.dumps({"stockitem": 2}))
        await send(message(photo="f3"))
        assert "P2PTD-00007" in tg.texts()[-1]

        # журнал для администратора
        await send(message(ui.JOURNAL))
        journal = tg.texts()[-1]
        assert "добавил" in journal and "P2PTD-00008" in journal and "вошёл" in journal

        # протухший токен → просьба войти заново
        store.save(USER.id, "anna", "expired")
        await send(message("P2PTD-00008"))
        assert "Войдите заново" in tg.texts()[-1] and store.get(USER.id) is None

        log_text = (tmp_path / "logs" / "bot.log").read_text(encoding="utf-8")
        assert "[user 500]" in log_text and "сохранено P2PTD-00008" in log_text
        assert "secret" not in log_text, "пароль не должен попадать в лог"
    finally:
        await api.close()
        server.close()


def test_full_scenario(tmp_path):
    asyncio.run(scenario(tmp_path))
