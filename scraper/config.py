"""Настройки scraper из окружения или .env в корне репозитория (тот же файл, что у бота и скриптов)."""

import os
from dataclasses import dataclass
from pathlib import Path

from bot import olegovic
from inventree_client import load_env


def _flag(name, default):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _ids(name):
    return frozenset(int(x) for x in os.environ.get(name, "").replace(" ", "").split(",") if x)


@dataclass(frozen=True)
class Config:
    inventree_url: str
    inventree_token: str
    data_dir: Path
    log_level: str
    manuals: bool             # искать мануалы
    photos: bool              # искать фото
    manual_search: bool       # искать мануалы в интернете (иначе только папка olegovic)
    manual_mode: str          # file — PDF во вложения, link — только ссылка
    olegovic_manuals: Path | None
    limit: int                # моделей за один запуск
    retry_days: int
    search: str               # ddgs | brave
    brave_key: str
    telegram_token: str
    admin_ids: frozenset
    verify: bool              # проверять находки моделью
    llm: dict | None          # url, model, think — модель для проверки (из olegovic-llm или LOCAL_LLM_*)


def load():
    load_env()
    env = os.environ.get
    missing = [k for k in ("INVENTREE_URL", "INVENTREE_TOKEN") if not env(k)]
    if missing:
        raise SystemExit(f"Не заданы переменные: {', '.join(missing)} (см. .env.example)")
    folder = env("SCRAPER_OLEGOVIC_MANUALS")
    if folder is None:
        folder = str(olegovic.manuals_folder())
    mode = env("SCRAPER_MANUAL_MODE", "file")
    if mode not in ("file", "link"):
        raise SystemExit(f"SCRAPER_MANUAL_MODE: file или link (сейчас {mode})")
    brave_key = env("BRAVE_API_KEY", "")
    verify = env("SCRAPER_VERIFY", "olegovic")
    if verify not in ("olegovic", "local", "off"):
        raise SystemExit(f"SCRAPER_VERIFY: olegovic, local или off (сейчас {verify})")
    llm = None
    if verify != "off":
        # модель для проверки: та же, что у olegovic-llm, или своя через LOCAL_LLM_URL / LOCAL_LLM_MODEL
        url, model, think = env("LOCAL_LLM_URL", ""), env("LOCAL_LLM_MODEL", ""), None
        if verify == "olegovic" and olegovic.config_path().exists():
            found = olegovic.load()
            url, model, think = url or found.url, model or found.model, found.think
        if url and model:
            llm = {"url": url, "model": model, "think": think, "timeout": float(env("LOCAL_LLM_TIMEOUT", "300"))}
    return Config(
        inventree_url=env("INVENTREE_URL").rstrip("/"),
        inventree_token=env("INVENTREE_TOKEN"),
        data_dir=Path(env("BOT_DATA_DIR", "bot_data")),
        log_level=env("BOT_LOG_LEVEL", "INFO").upper(),
        manuals=_flag("SCRAPER_MANUALS", "true"),
        photos=_flag("SCRAPER_PHOTOS", "true"),
        manual_search=_flag("SCRAPER_MANUAL_SEARCH", "true"),
        manual_mode=mode,
        olegovic_manuals=Path(folder).expanduser() if folder else None,
        limit=int(env("SCRAPER_LIMIT", "20")),
        retry_days=int(env("SCRAPER_RETRY_DAYS", "30")),
        search=env("SCRAPER_SEARCH", "brave" if brave_key else "ddgs"),
        brave_key=brave_key,
        telegram_token=env("TELEGRAM_BOT_TOKEN", ""),
        admin_ids=_ids("BOT_ADMIN_IDS"),
        verify=verify != "off" and llm is not None,
        llm=llm,
    )
