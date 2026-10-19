"""Логи бота.

* Технический лог: консоль + файл bot_data/logs/bot.log (ротация 5 × 5 МБ).
  В каждой строке — Telegram ID пользователя, от которого пришло событие.
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


def setup(data_dir, level="INFO", filename="bot.log"):
    log_dir = Path(data_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers = [
        logging.StreamHandler(),
        RotatingFileHandler(log_dir / filename, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"),
    ]
    user_filter = _UserFilter()
    for h in handlers:
        h.addFilter(user_filter)
        h.setFormatter(logging.Formatter(FORMAT))
    logging.basicConfig(level=level, handlers=handlers, force=True)
    # Библиотеки пишут слишком подробно на INFO
    for noisy in ("aiogram.event", "httpx", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
