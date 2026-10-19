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
from .labels import Labels
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


def build_scraper(api, store):
    """Каталог (ночной поиск мануалов и фото) для кнопки «🗂 Каталог»; None, если не настроен."""
    try:
        from scraper import config as scraper_config
        from scraper.engine import build
        cfg = scraper_config.load()
    except ImportError as e:
        log.warning("каталог выключен: нет зависимостей (%s) — pip install -r scraper/requirements.txt", e)
        return None
    except SystemExit as e:
        log.info("каталог выключен: %s", e)
        return None
    return build(cfg, api, store)


def build_recognizer(cfg):
    engines = []
    for name in cfg.recognizers:
        if name == "claude":
            engines.append(ClaudeRecognizer(cfg.claude_model, cfg.claude_effort))
        else:
            engines.append(LocalRecognizer(cfg.local_llm_url, cfg.local_llm_model, cfg.local_llm_key,
                                           cfg.local_llm_timeout, cfg.local_llm_think))
    return ChainRecognizer(engines)


async def main():
    cfg = config.load()
    logs.setup(cfg.data_dir, cfg.log_level, extra={"scraper": "scraper.log"})  # прогоны из бота — и в лог каталога
    api = Api(cfg.inventree_url)
    recognizer = build_recognizer(cfg)
    store = Storage(Path(cfg.data_dir) / "bot.sqlite3")
    scraper = build_scraper(api, store)
    bot = Bot(cfg.telegram_token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(
        storage=MemoryStorage(),
        cfg=cfg,
        api=api,
        store=store,
        scraper=scraper,
        catalog=Catalog(),
        numbering=Numbering(cfg.serial_prefix, cfg.serial_digits),
        recognizer=recognizer,
        photos=PhotoBuffer(),
        labels=Labels(cfg.label_template),
    )
    dp.message.outer_middleware(Context())
    dp.callback_query.outer_middleware(Context())
    dp.errors.register(common.on_error)
    dp.include_routers(*routers())

    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
    log.info("бот запущен: InvenTree %s, распознавание %s", cfg.inventree_url, " → ".join(cfg.recognizers))
    if "local" in cfg.recognizers:
        log.info("своя модель: %s на %s (настройки: %s)", cfg.local_llm_model, cfg.local_llm_url, cfg.local_llm_source)
    for engine, problem in (await recognizer.check()).items():
        log.warning("распознавание %s: %s", engine, problem)
    try:
        await dp.start_polling(bot)
    finally:
        await api.close()
        await recognizer.close()
        if scraper:
            await scraper.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
