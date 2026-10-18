"""Настройки бота из окружения или .env в корне репозитория."""

import os
from dataclasses import dataclass

from inventree_client import load_env


@dataclass
class Config:
    telegram_token: str
    inventree_url: str
    allowed_ids: set
    db_path: str
    claude_model: str
    serial_prefix: str
    serial_digits: int


def load():
    load_env()
    missing = [k for k in ("TELEGRAM_BOT_TOKEN", "INVENTREE_URL", "ANTHROPIC_API_KEY") if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"Не заданы переменные: {', '.join(missing)} (см. .env.example)")
    allowed = {int(x) for x in os.environ.get("BOT_ALLOWED_TELEGRAM_IDS", "").replace(" ", "").split(",") if x}
    return Config(
        telegram_token=os.environ["TELEGRAM_BOT_TOKEN"],
        inventree_url=os.environ["INVENTREE_URL"].rstrip("/"),
        allowed_ids=allowed,
        db_path=os.environ.get("BOT_DB_PATH", "bot_data/bot.sqlite3"),
        claude_model=os.environ.get("BOT_CLAUDE_MODEL", "claude-opus-5-5"),
        serial_prefix=os.environ.get("BOT_SERIAL_PREFIX", "P2PTD-"),
        serial_digits=int(os.environ.get("BOT_SERIAL_DIGITS", "5")),
    )
