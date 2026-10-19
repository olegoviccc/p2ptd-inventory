"""«🐞 Диагностика»: один текстовый файл, который администратор пересылает разработчику.

Версия, настройки, проверка связи с InvenTree и моделью, состояние каталога, журнал и хвосты логов
с полными трассировками ошибок. Токены и ключи вырезаются — файл можно пересылать.
"""

import asyncio
import os
import platform
import re
import subprocess
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

from . import logs, ui

STARTED = time.time()
ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ("aiogram", "aiohttp", "anthropic", "ddgs", "pypdf", "pillow", "zxing-cpp")
SECRET_ENV = ("TELEGRAM_BOT_TOKEN", "INVENTREE_TOKEN", "ANTHROPIC_API_KEY", "BRAVE_API_KEY", "TAVILY_API_KEY",
              "LOCAL_LLM_API_KEY")
LOG_FILES = ("bot.log", "scraper.log")
HIDDEN = "«скрыто»"

_TELEGRAM = re.compile(r"(?<!\d)\d{6,}:[A-Za-z0-9_-]{30,}")  # и внутри URL: api.telegram.org/bot<токен>/
_PREFIXED = re.compile(r"\b(?:inv|sk|tvly)-[A-Za-z0-9_-]{10,}")
_AUTH = re.compile(r"(?i)\b(token|bearer|basic) [A-Za-z0-9+/=_.-]{10,}")
_ASSIGN = re.compile(r"(?i)\b(api[_-]?key|token|password|x-api-key)([\"']?\s*[:=]\s*[\"']?)[^\s\"',&]{6,}")


def mask(text, known=()):
    """Убрать из текста токены и ключи: известные значения и всё, что на них похоже."""
    for value in sorted({v for v in known if v and len(v) >= 6}, key=len, reverse=True):
        text = text.replace(value, HIDDEN)
    text = _TELEGRAM.sub(HIDDEN, text)
    text = _PREFIXED.sub(HIDDEN, text)
    text = _AUTH.sub(lambda m: f"{m.group(1)} {HIDDEN}", text)
    return _ASSIGN.sub(lambda m: m.group(1) + m.group(2) + HIDDEN, text)


def known_secrets(store):
    """Значения секретов из окружения и токены пользователей бота."""
    values = [os.environ.get(k, "") for k in SECRET_ENV]
    values += [row[0] for row in store.db.execute("SELECT token FROM users")]
    return values


def log_path(data_dir, name):
    return logs.log_dir(data_dir) / name


def tail(path, lines=300):
    """Последние строки лога; если файл только что сменился при ротации — с концом предыдущего."""
    collected = []
    for p in (path, path.with_name(path.name + ".1")):
        if not p.exists():
            continue
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 400_000))
            chunk = f.read().decode("utf-8", "replace").splitlines()
        collected = chunk[-(lines - len(collected)):] + collected
        if len(collected) >= lines:
            break
    return collected[-lines:]


def _git():
    def git(*args):
        out = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    try:
        head = git("log", "-1", "--format=%h %cd %s", "--date=short")
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
        changed = git("status", "--porcelain", "--untracked-files=no").splitlines()
    except (OSError, subprocess.SubprocessError) as e:
        return f"git недоступен: {e}"
    if not head:
        return "не git-репозиторий"
    return f"{head} (ветка {branch})" + (f"; изменены локально: {', '.join(c[3:] for c in changed)}" if changed else "")


def _versions():
    found = []
    for name in PACKAGES:
        try:
            found.append(f"{name} {metadata.version(name)}")
        except metadata.PackageNotFoundError:
            found.append(f"{name} —")
    return ", ".join(found)


async def _safe(coro, timeout=60):
    try:
        return await asyncio.wait_for(coro, timeout)
    except Exception as e:  # noqa: BLE001 — диагностика собирается целиком, даже если что-то сломано
        return f"ОШИБКА {type(e).__name__}: {e}"


def _uptime():
    minutes = int(time.time() - STARTED) // 60
    return f"{minutes // 1440} д {minutes // 60 % 24} ч {minutes % 60} мин"


def _config(cfg):
    lines = []
    for key, value in (asdict(cfg) if is_dataclass(cfg) else vars(cfg)).items():
        if any(word in key for word in ("token", "key", "password")):
            value = "задан" if value else "не задан"
        elif isinstance(value, (set, frozenset, tuple)):
            value = ", ".join(map(str, sorted(value) if isinstance(value, (set, frozenset)) else value)) or "—"
        lines.append(f"  {key}: {value}")
    return lines


async def _inventree(it):
    info = await it.get("/api/")
    me = await it.get("/api/user/me/")
    return (f"отвечает, InvenTree {info.get('version', '?')}, API {info.get('apiVersion', '?')}, "
            f"пользователь {me.get('username', '?')}")


async def _catalog(scraper):
    lines = []
    settings = scraper.settings()
    lines.append(f"  настройки: {settings}")
    lines.append(f"  поиск: {scraper.search.name}, режим мануалов: {scraper.cfg.manual_mode}, "
                 f"папка olegovic: {scraper.cfg.olegovic_manuals}")
    lines.append(f"  InvenTree от имени INVENTREE_TOKEN: {await _safe(_inventree(scraper.it), 30)}")
    if scraper.llm:
        problem = await _safe(scraper.llm.check(), 30)
        lines.append(f"  модель проверки {scraper.llm.model} на {scraper.llm.base}: {problem or 'отвечает'}")
    else:
        lines.append("  модель проверки: не настроена")
    stats = await _safe(scraper.stats(), 120)
    if isinstance(stats, str):
        lines.append(f"  состояние каталога: {stats}")
    else:
        lines.append(f"  моделей {stats.parts} (без кода модели {stats.no_model}), без мануала {stats.missing_manual}, "
                     f"без фото {stats.missing_photo}")
    if scraper.progress is not None:
        p = scraper.progress
        lines.append(f"  сейчас идёт прогон: {p.checked} из {p.total}, ошибок {len(p.errors)}")
    reviews = scraper.state.reviews()
    lines.append(f"  ждут проверки: {len(reviews)}"
                 + "".join(f"\n    {r.part_name} — {r.kind}: {r.url or r.path.name} ({r.reason})" for r in reviews[:20]))
    lines.append("  последние прогоны:")
    for r in scraper.state.runs(10):
        extra = {k: v for k, v in r.items() if k not in ("started", "finished", "trigger")}
        lines.append(f"    {r['started'][:16]} → {(r['finished'] or 'не закончен')[:16]} {r['trigger']}: {extra}")
    return lines


def _journal(store, n=50):
    lines = []
    for e in store.recent_events(n):
        what = ", ".join(f"{k}: {v}" for k, v in e.details.items() if v not in ("", None))
        lines.append(f"  {e.ts[:19]} {e.username or e.tg_id} {ui.EVENT_NAMES.get(e.action, e.action)} {what}")
    return lines or ["  пусто"]


async def report(cfg, store, recognizer, scraper=None, it=None):
    """Текст диагностики (секреты уже вырезаны)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    git = await asyncio.to_thread(_git)
    out = ["P2PTD inventory — диагностика", f"Время: {now} UTC, бот работает {_uptime()}",
           f"Версия: {git}", f"Python {platform.python_version()}, {platform.platform()}", f"Пакеты: {_versions()}",
           "", "== Настройки бота ==", *_config(cfg), "", "== Проверки =="]
    if it:
        out.append(f"  InvenTree {cfg.inventree_url} от вашего имени: {await _safe(_inventree(it), 30)}")
    checks = await _safe(recognizer.check(), 60) if hasattr(recognizer, "check") else {}
    if isinstance(checks, dict):
        out.append(f"  распознавание ({' → '.join(cfg.recognizers)}): "
                   + ("; ".join(f"{k}: {v}" for k, v in checks.items()) or "всё отвечает"))
    else:
        out.append(f"  распознавание: {checks}")
    out += ["", "== Каталог (ночной поиск) =="]
    out += await _catalog(scraper) if scraper else ["  не настроен (нет INVENTREE_TOKEN или зависимостей scraper)"]
    out += ["", "== Журнал (последние 50) ==", *_journal(store)]
    for name in LOG_FILES:
        lines = await asyncio.to_thread(tail, log_path(cfg.data_dir, name))
        out += ["", f"== {name} (последние {len(lines)} строк) ==", *(lines or ["  файла нет или пуст"])]
    return mask("\n".join(out) + "\n", known_secrets(store))


async def log_file(cfg, store, name):
    """Весь текущий файл лога (до 5 МБ) с вырезанными секретами или None."""
    path = log_path(cfg.data_dir, name)
    if name not in LOG_FILES or not path.exists():
        return None
    text = await asyncio.to_thread(path.read_text, "utf-8", "replace")
    return mask(text, known_secrets(store))
