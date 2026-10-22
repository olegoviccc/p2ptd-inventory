"""Подключение к модели, которую уже запускает olegovic-llm на этом же ПК.

Читаем только его config.toml (секреты из secrets.env не трогаем) и берём:
* адрес сервера моделей (llama-swap на 127.0.0.1:8080 или Ollama на 11434) —
  у обоих есть OpenAI-совместимый /v1/chat/completions;
* модель роли vision (пусто = smart), чтобы llama-swap не перезагружал модели
  между запросами olegovic и нашего бота;
* настройку think для этой модели из [llm.think].

Значения по умолчанию совпадают с olegovic (src/olegovic/config.py).
"""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG = "~/.config/olegovic/config.toml"
DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_SMART = "gpt-oss:20b"


@dataclass(frozen=True)
class OlegovicLLM:
    url: str      # .../v1
    model: str
    think: object  # None, True/False или "low"/"medium"/"high"
    source: Path


def config_path():
    explicit = os.environ.get("OLEGOVIC_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    config_dir = os.environ.get("OLEGOVIC_CONFIG_DIR")
    return Path(config_dir).expanduser() / "config.toml" if config_dir else Path(DEFAULT_CONFIG).expanduser()


def load(path=None):
    path = Path(path).expanduser() if path else config_path()
    if not path.exists():
        raise SystemExit(f"Не найден конфиг olegovic-llm: {path}. Укажите OLEGOVIC_CONFIG или LOCAL_LLM_URL/LOCAL_LLM_MODEL.")
    llm = tomllib.loads(path.read_text(encoding="utf-8")).get("llm", {})
    models = llm.get("models", {})
    model = models.get("vision") or models.get("smart") or DEFAULT_SMART
    base = (llm.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    return OlegovicLLM(url=base if base.endswith("/v1") else base + "/v1", model=model,
                       think=llm.get("think", {}).get(model), source=path)


DEFAULT_MANUALS = "~/olegovic/out/manuals"


def manuals_folder(path=None):
    """Папка, куда olegovic-llm складывает найденные мануалы (<Производитель>/<MPN>.pdf).

    Без конфига olegovic — его папка по умолчанию; scraper просто не найдёт там файлов, если её нет.
    """
    path = Path(path).expanduser() if path else config_path()
    manuals = {}
    if path.exists():
        manuals = tomllib.loads(path.read_text(encoding="utf-8")).get("manuals", {})
    return Path(manuals.get("folder") or DEFAULT_MANUALS).expanduser()
