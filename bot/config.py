"""Настройки бота из окружения или .env в корне репозитория."""

import os
from dataclasses import dataclass

from inventree_client import load_env


def _ids(name):
    return {int(x) for x in os.environ.get(name, "").replace(" ", "").split(",") if x}


@dataclass(frozen=True)
class Config:
    telegram_token: str
    inventree_url: str
    allowed_ids: frozenset
    admin_ids: frozenset
    data_dir: str
    log_level: str
    recognizers: tuple  # порядок попыток: ("local", "claude")
    claude_model: str
    claude_effort: str
    local_llm_url: str
    local_llm_model: str
    local_llm_key: str
    local_llm_timeout: float
    serial_prefix: str
    serial_digits: int


def load():
    load_env()
    env = os.environ.get
    recognizers = tuple(x.strip() for x in env("BOT_RECOGNIZER", "claude").split(",") if x.strip())
    required = ["TELEGRAM_BOT_TOKEN", "INVENTREE_URL"]
    if "claude" in recognizers:
        required.append("ANTHROPIC_API_KEY")
    if "local" in recognizers:
        required += ["LOCAL_LLM_URL", "LOCAL_LLM_MODEL"]
    unknown = set(recognizers) - {"claude", "local"}
    missing = [k for k in required if not env(k)]
    if unknown or not recognizers:
        raise SystemExit(f"BOT_RECOGNIZER: допустимо claude, local или local,claude (сейчас {recognizers})")
    if missing:
        raise SystemExit(f"Не заданы переменные: {', '.join(missing)} (см. .env.example)")
    admins = _ids("BOT_ADMIN_IDS")
    allowed = _ids("BOT_ALLOWED_TELEGRAM_IDS")
    return Config(
        telegram_token=env("TELEGRAM_BOT_TOKEN"),
        inventree_url=env("INVENTREE_URL").rstrip("/"),
        allowed_ids=frozenset(allowed | admins if allowed else set()),
        admin_ids=frozenset(admins),
        data_dir=env("BOT_DATA_DIR", "bot_data"),
        log_level=env("BOT_LOG_LEVEL", "INFO").upper(),
        recognizers=recognizers,
        claude_model=env("BOT_CLAUDE_MODEL", "claude-opus-5-5"),
        claude_effort=env("BOT_CLAUDE_EFFORT", "low"),
        local_llm_url=env("LOCAL_LLM_URL", "").rstrip("/"),
        local_llm_model=env("LOCAL_LLM_MODEL", ""),
        local_llm_key=env("LOCAL_LLM_API_KEY", ""),
        local_llm_timeout=float(env("LOCAL_LLM_TIMEOUT", "60")),
        serial_prefix=env("BOT_SERIAL_PREFIX", "P2PTD-"),
        serial_digits=int(env("BOT_SERIAL_DIGITS", "5")),
    )
