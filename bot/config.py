"""Настройки бота из окружения или .env в корне репозитория."""

import os
from dataclasses import dataclass

from inventree_client import load_env

from . import olegovic


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
    local_llm_think: object  # None, True/False или "low"/"medium"/"high"
    local_llm_source: str    # откуда взяты настройки своей модели (для лога)
    serial_prefix: str
    serial_digits: int


def _think(value):
    """LOCAL_LLM_THINK: пусто — не передавать, true/false — Qwen-шаблоны, low/medium/high — gpt-oss."""
    value = (value or "").strip().lower()
    return {"": None, "true": True, "false": False}.get(value, value)


def load():
    load_env()
    env = os.environ.get
    recognizers = tuple(x.strip() for x in env("BOT_RECOGNIZER", "claude").split(",") if x.strip())
    unknown = set(recognizers) - {"claude", "local", "olegovic"}
    if unknown or not recognizers or {"local", "olegovic"} <= set(recognizers):
        raise SystemExit("BOT_RECOGNIZER: допустимо claude, olegovic, local или цепочка вроде olegovic,claude "
                         f"(сейчас {recognizers})")

    # olegovic = своя модель, настройки которой берутся из config.toml olegovic-llm на этом ПК
    local_url, local_model, local_think = env("LOCAL_LLM_URL", ""), env("LOCAL_LLM_MODEL", ""), _think(env("LOCAL_LLM_THINK"))
    source = "LOCAL_LLM_*"
    if "olegovic" in recognizers:
        found = olegovic.load()
        local_url = local_url or found.url
        local_model = local_model or found.model
        local_think = local_think if env("LOCAL_LLM_THINK") else found.think
        source = str(found.source)
        recognizers = tuple("local" if r == "olegovic" else r for r in recognizers)

    required = ["TELEGRAM_BOT_TOKEN", "INVENTREE_URL"]
    if "claude" in recognizers:
        required.append("ANTHROPIC_API_KEY")
    missing = [k for k in required if not env(k)]
    if "local" in recognizers and not (local_url and local_model):
        missing += ["LOCAL_LLM_URL", "LOCAL_LLM_MODEL"]
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
        local_llm_url=local_url.rstrip("/"),
        local_llm_model=local_model,
        local_llm_key=env("LOCAL_LLM_API_KEY", ""),
        # llama-swap может сначала загружать модель с диска — даём время
        local_llm_timeout=float(env("LOCAL_LLM_TIMEOUT", "300")),
        local_llm_think=local_think,
        local_llm_source=source,
        serial_prefix=env("BOT_SERIAL_PREFIX", "P2PTD"),
        serial_digits=int(env("BOT_SERIAL_DIGITS", "4")),
    )
