"""Логи бота.

* Технический лог: консоль + файл bot_data/logs/bot.log (ротация 5 × 5 МБ).
  В каждой строке — Telegram ID пользователя, от которого пришло событие.
* Лог каталога (scraper): bot_data/logs/scraper.log — и ночные прогоны, и запущенные из бота.
* Оба файла администратор может получить в боте: «🐞 Диагностика».
* Журнал действий (кто что добавил, распознал, нашёл) хранится в SQLite
  (storage.Storage.log_event) и виден администраторам в боте кнопкой «📜 Журнал».
"""

import contextvars
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

current_user = contextvars.ContextVar("current_user", default="-")

FORMAT = "%(asctime)s %(levelname)-7s %(name)s [user %(user)s] %(message)s"


class _UserFilter(logging.Filter):
    def filter(self, record):
        record.user = current_user.get()
        return True


def log_dir(data_dir):
    return Path(data_dir) / "logs"


def _prepare(handler, user_filter):
    handler.addFilter(user_filter)
    handler.setFormatter(logging.Formatter(FORMAT))
    return handler


def _file(path):
    return RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")


def setup(data_dir, level="INFO", filename="bot.log", extra=None):
    """extra: {"scraper": "scraper.log"} — логгер, который дополнительно пишется в свой файл."""
    folder = log_dir(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    user_filter = _UserFilter()
    handlers = [_prepare(logging.StreamHandler(), user_filter), _prepare(_file(folder / filename), user_filter)]
    logging.basicConfig(level=level, handlers=handlers, force=True)
    for name, extra_file in (extra or {}).items():
        logger = logging.getLogger(name)
        for h in list(logger.handlers):
            logger.removeHandler(h)
            h.close()
        logger.addHandler(_prepare(_file(folder / extra_file), user_filter))
    # Библиотеки пишут слишком подробно на INFO
    for noisy in ("aiogram.event", "httpx", "anthropic", "primp", "ddgs"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # pypdf на каждом PDF со шрифтами-кривульками пишет сотни строк «Ignoring final token…»
    logging.getLogger("pypdf").setLevel(logging.ERROR)
