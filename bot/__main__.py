"""Запуск: python -m bot"""

import asyncio
import logging

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from . import config
from .handlers import router
from .numbering import Numbering
from .recognize import Recognizer
from .storage import Storage


class AllowList(BaseMiddleware):
    """Если задан BOT_ALLOWED_TELEGRAM_IDS, остальным бот не отвечает."""

    def __init__(self, allowed):
        self.allowed = allowed

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if self.allowed and (not user or user.id not in self.allowed):
            logging.info("отклонён пользователь %s", user.id if user else None)
            return None
        return await handler(event, data)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config.load()
    bot = Bot(cfg.telegram_token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(
        storage=MemoryStorage(),
        cfg=cfg,
        store=Storage(cfg.db_path),
        numbering=Numbering(cfg.serial_prefix, cfg.serial_digits),
        recognizer=Recognizer(cfg.claude_model),
    )
    allow = AllowList(cfg.allowed_ids)
    dp.message.outer_middleware(allow)
    dp.callback_query.outer_middleware(allow)
    dp.include_router(router)
    await bot.set_my_commands([
        BotCommand(command="add", description="Добавить прибор по фото"),
        BotCommand(command="login", description="Войти аккаунтом InvenTree"),
        BotCommand(command="cancel", description="Отменить"),
        BotCommand(command="help", description="Помощь"),
    ])
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
