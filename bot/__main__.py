"""Запуск: python -m bot"""

import asyncio
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from . import config, logs
from .handlers import common, routers
from .inventree import Api, Catalog
from .middlewares import Context
from .numbering import Numbering
from .photos import PhotoBuffer
from .recognize import ChainRecognizer, ClaudeRecognizer, LocalRecognizer
from .storage import Storage

log = logging.getLogger("bot")

COMMANDS = [
    ("start", "Главное меню"),
    ("add", "Добавить прибор по фото"),
    ("find", "Найти позицию"),
    ("place", "Сменить место"),
    ("profile", "Профиль и выход"),
    ("cancel", "Отменить"),
]


def build_recognizer(cfg):
    engines = []
    for name in cfg.recognizers:
        if name == "claude":
            engines.append(ClaudeRecognizer(cfg.claude_model, cfg.claude_effort))
        else:
            engines.append(LocalRecognizer(cfg.local_llm_url, cfg.local_llm_model, cfg.local_llm_key,
                                           cfg.local_llm_timeout))
    return ChainRecognizer(engines)


async def main():
    cfg = config.load()
    logs.setup(cfg.data_dir, cfg.log_level)
    api = Api(cfg.inventree_url)
    recognizer = build_recognizer(cfg)
    bot = Bot(cfg.telegram_token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(
        storage=MemoryStorage(),
        cfg=cfg,
        api=api,
        store=Storage(Path(cfg.data_dir) / "bot.sqlite3"),
        catalog=Catalog(),
        numbering=Numbering(cfg.serial_prefix, cfg.serial_digits),
        recognizer=recognizer,
        photos=PhotoBuffer(),
    )
    dp.message.outer_middleware(Context())
    dp.callback_query.outer_middleware(Context())
    dp.errors.register(common.on_error)
    dp.include_routers(*routers())

    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
    log.info("бот запущен: InvenTree %s, распознавание %s", cfg.inventree_url, " → ".join(cfg.recognizers))
    try:
        await dp.start_polling(bot)
    finally:
        await api.close()
        await recognizer.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
