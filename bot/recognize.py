"""Распознавание оборудования по фото.

Движки:
* ClaudeRecognizer — Claude API (vision + structured outputs);
* LocalRecognizer — своя модель с OpenAI-совместимым API (Ollama, LM Studio,
  llama.cpp server, vLLM). Модель должна уметь работать с изображениями;
* ChainRecognizer — пробует движки по очереди (например, сначала локальный,
  при ошибке или недоступности — Claude).
"""

import base64
import json
import logging
import re
import time
from dataclasses import dataclass, fields

import aiohttp
import anthropic

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
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        data = json.loads(text)
    except ValueError as e:
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
        self.base = base_url.rstrip("/")
        self.url = self.base + "/chat/completions"
        self.model = model
        self.think = think
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session = None

    @property
    def session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def check(self):
        """Проверка при старте: сервер отвечает и знает модель. Возвращает текст проблемы или None."""
        try:
            async with self.session.get(self.base + "/models", headers=self.headers,
                                        timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status >= 400:
                    return f"{self.base}/models ответил {resp.status}"
                ids = [m.get("id") for m in (await resp.json(content_type=None)).get("data", [])]
        except (aiohttp.ClientError, TimeoutError, ValueError) as e:
            return f"нет связи с {self.base}: {e}"
        if ids and self.model not in ids:
            return f"модели {self.model} нет на сервере (есть: {', '.join(ids[:8])})"
        return None

    async def recognize(self, photos, categories, known_models=(), barcodes=()):
        started = time.monotonic()
        content = [{"type": "text", "text": prompt(categories, known_models, barcodes)}]
        content += [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64(p)}"}} for p in photos]
        body = {
            "model": self.model,
            "temperature": 0,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 4096,
            # strict=False — как в olegovic-llm: llama-server и Ollama держат схему грамматикой
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "recognition", "strict": False, "schema": schema(categories)}},
        }
        if isinstance(self.think, bool):  # Qwen-шаблоны: размышления вкл/выкл
            body["chat_template_kwargs"] = {"enable_thinking": self.think}
        elif self.think:                  # gpt-oss: low / medium / high
            body["reasoning_effort"] = self.think
            body["chat_template_kwargs"] = {"reasoning_effort": self.think}
        try:
            async with self.session.post(self.url, json=body, headers=self.headers) as resp:
                if resp.status >= 400:
                    raise RecognitionError(f"local: сервер ответил {resp.status}: {(await resp.text())[:200]}")
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as e:
            raise RecognitionError(f"local: нет связи с {self.url}") from e
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise RecognitionError("local: неожиданный формат ответа") from e
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
