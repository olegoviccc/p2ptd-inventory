"""Распознавание оборудования по фото.

Движки:
* ClaudeRecognizer — Claude API (vision + structured outputs);
* LocalRecognizer — своя модель с OpenAI-совместимым API (Ollama, LM Studio,
  llama.cpp server, vLLM). Модель должна уметь работать с изображениями;
* ChainRecognizer — пробует движки по очереди (например, сначала локальный,
  при ошибке или недоступности — Claude).
"""

import base64
import logging
import time
from dataclasses import dataclass, fields

import anthropic

from .llm import LLMError, LocalLLM, extract_json, image_part

log = logging.getLogger(__name__)

UNKNOWN_CATEGORY = "Не определено"

PROMPT = """You are cataloguing professional sound, light, video, DJ and IT equipment for a venue inventory.
The photos show one device: usually its nameplate/label and sometimes an overall view.

Extract:
- manufacturer: brand as printed (e.g. "Pioneer DJ", "Yamaha", "Behringer"); "" if unknown.
- model: exact model code as printed on the device (e.g. "XDJ-1000MK2"), not a series name; "" if unknown.
- serial: the manufacturer's serial number (S/N, Serial No., SN). Copy it character by character.
  Never guess or complete digits. If it is not clearly readable, return "".
- category: the best matching category from the allowed list, or "{unknown}".
- overview_photo: 0-based index of the photo that shows the whole device best (for the catalogue picture), -1 if none.
- confidence: "high" if manufacturer, model and serial are all clearly readable; "medium" if something is
  inferred from the device's appearance; "low" if the photos are unclear.
- notes: short note in Russian about anything the person should check (e.g. "серийник частично закрыт").

Allowed categories:
{categories}

{known}Barcodes decoded from the photos (exact, use them to confirm the serial): {barcodes}

Answer with JSON only."""


@dataclass
class Recognition:
    manufacturer: str
    model: str
    serial: str
    category: str
    overview_photo: int
    confidence: str
    notes: str
    engine: str = ""
    elapsed_ms: int = 0

    @property
    def name(self):
        return " ".join(p for p in (self.manufacturer, self.model) if p)


FIELDS = [f.name for f in fields(Recognition) if f.name not in ("engine", "elapsed_ms")]


class RecognitionError(Exception):
    pass


def schema(categories):
    return {
        "type": "object",
        "properties": {
            "manufacturer": {"type": "string"},
            "model": {"type": "string"},
            "serial": {"type": "string"},
            "category": {"type": "string", "enum": [*categories, UNKNOWN_CATEGORY]},
            "overview_photo": {"type": "integer"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "notes": {"type": "string"},
        },
        "required": FIELDS,
        "additionalProperties": False,
    }


def prompt(categories, known_models, barcodes):
    known = ""
    if known_models:
        known = "Models already in the inventory (prefer these exact spellings if it is one of them):\n"
        known += "\n".join(f"- {m}" for m in known_models) + "\n\n"
    return PROMPT.format(unknown=UNKNOWN_CATEGORY, categories="\n".join(f"- {c}" for c in categories),
                         known=known, barcodes=", ".join(barcodes) or "none")


def finalize(text, photo_count, categories, engine, started):
    """JSON ответа модели → Recognition с проверкой значений (локальные модели схему не гарантируют)."""
    try:
        data = extract_json(text)
    except ValueError as e:
        log.warning("%s: не разобран ответ модели: %r", engine, (text or "")[:500])
        raise RecognitionError(f"{engine}: ответ не JSON") from e
    if not isinstance(data, dict):
        raise RecognitionError(f"{engine}: ответ не объект JSON")
    values = {k: data.get(k) for k in FIELDS}
    for key in ("manufacturer", "model", "serial", "notes"):
        values[key] = str(values[key] or "").strip()
    if values["category"] not in categories:
        values["category"] = UNKNOWN_CATEGORY
    if values["confidence"] not in ("high", "medium", "low"):
        values["confidence"] = "low"
    idx = values["overview_photo"]
    values["overview_photo"] = idx if isinstance(idx, int) and 0 <= idx < photo_count else -1
    return Recognition(**values, engine=engine, elapsed_ms=int((time.monotonic() - started) * 1000))


def b64(data):
    return base64.standard_b64encode(data).decode()


class ClaudeRecognizer:
    name = "claude"

    def __init__(self, model, effort="low", client=None):
        self.model = model
        self.effort = effort
        self.client = client or anthropic.AsyncAnthropic()

    async def recognize(self, photos, categories, known_models=(), barcodes=()):
        started = time.monotonic()
        content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64(p)}}
                   for p in photos]
        content.append({"type": "text", "text": prompt(categories, known_models, barcodes)})
        try:
            response = await self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"effort": self.effort,
                               "format": {"type": "json_schema", "schema": schema(categories)}},
                messages=[{"role": "user", "content": content}],
            )
        except anthropic.APIConnectionError as e:
            raise RecognitionError("claude: нет связи с API") from e
        except anthropic.APIStatusError as e:
            raise RecognitionError(f"claude: API ответил {e.status_code}") from e
        if response.stop_reason == "refusal":
            raise RecognitionError("claude: модель отказалась обрабатывать фото")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            raise RecognitionError(f"claude: пустой ответ ({response.stop_reason})")
        return finalize(text, len(photos), categories, self.name, started)


class LocalRecognizer:
    """Своя модель через OpenAI-совместимый /v1/chat/completions."""

    name = "local"

    def __init__(self, base_url, model, api_key="", timeout=300, think=None):
        self.llm = LocalLLM(base_url, model, api_key, timeout, think)

    async def close(self):
        await self.llm.close()

    async def check(self):
        """Проверка при старте: сервер отвечает и знает модель. Возвращает текст проблемы или None."""
        return await self.llm.check()

    async def recognize(self, photos, categories, known_models=(), barcodes=()):
        started = time.monotonic()
        content = [{"type": "text", "text": prompt(categories, known_models, barcodes)}]
        content += [image_part(p) for p in photos]
        try:
            text = await self.llm.complete(content, schema(categories), "recognition")
        except LLMError as e:
            raise RecognitionError(f"local: {e}") from e
        return finalize(text, len(photos), categories, self.name, started)


class ChainRecognizer:
    """Пробует движки по очереди, пока какой-то не ответит."""

    def __init__(self, engines):
        self.engines = engines

    async def recognize(self, photos, categories, known_models=(), barcodes=()):
        errors = []
        for engine in self.engines:
            try:
                return await engine.recognize(photos, categories, known_models, barcodes)
            except RecognitionError as e:
                log.warning("распознавание не удалось: %s", e)
                errors.append(str(e))
        raise RecognitionError("; ".join(errors))

    async def check(self):
        problems = {}
        for engine in self.engines:
            if hasattr(engine, "check"):
                problem = await engine.check()
                if problem:
                    problems[engine.name] = problem
        return problems

    async def close(self):
        for engine in self.engines:
            if hasattr(engine, "close"):
                await engine.close()
