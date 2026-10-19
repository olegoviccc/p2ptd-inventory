"""Проверка найденного моделью olegovic-llm: то ли это устройство, руководство ли это к нему.

Решение модели:
* match — прикладываем;
* mismatch — отбрасываем, ищем следующий вариант;
* unsure — в очередь «На проверку» в боте, решает человек.
"""

import logging
from dataclasses import dataclass

from bot.llm import LLMError, extract_json, image_part

log = logging.getLogger(__name__)

MATCH, MISMATCH, UNSURE = "match", "mismatch", "unsure"

SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string", "enum": [MATCH, MISMATCH, UNSURE]},
                   "reason": {"type": "string"}},
    "required": ["verdict", "reason"],
    "additionalProperties": False,
}

PHOTO_PROMPT = """You check product photos for an equipment inventory.
Expected device: {manufacturer} {model}.

Is this image a product photo of exactly this device?
- "match": the photo clearly shows this model (product shot, the model name/shape fits).
- "mismatch": another product or model, a logo, a person, a room, a rack of many devices, a screenshot, a banner.
- "unsure": it could be this device but you cannot tell (similar models of the same series, no visible markings).
Answer with JSON: verdict and a short reason in Russian."""

MANUAL_PROMPT = """You check PDF documents for an equipment inventory.
Expected: the user/owner/operating manual (or quick start guide) for {manufacturer} {model}.

Text from the first pages of the PDF:
---
{text}
---

- "match": a user/owner/operating manual or quick start guide for exactly this model
  (a manual covering several models counts if this model is named).
- "mismatch": another model, or not a user manual (datasheet, spec sheet, brochure, catalogue,
  service manual, declaration of conformity, price list).
- "unsure": cannot tell from this text.
Answer with JSON: verdict and a short reason in Russian."""


@dataclass
class Verdict:
    verdict: str
    reason: str = ""


class Verifier:
    """Проверка моделью. При недоступной модели: мануал (код модели в тексте уже проверен) — match,
    фото — unsure, чтобы непроверенная картинка не попала в каталог сама."""

    def __init__(self, llm):
        self.llm = llm

    async def _ask(self, content, fallback):
        try:
            data = extract_json(await self.llm.complete(content, SCHEMA, "verdict", max_tokens=2048))
        except (LLMError, ValueError) as e:
            log.warning("проверка моделью не удалась: %s", e)
            return Verdict(fallback, f"модель недоступна: {e}"[:120])
        verdict = data.get("verdict") if isinstance(data, dict) else None
        if verdict not in (MATCH, MISMATCH, UNSURE):
            return Verdict(UNSURE, "модель ответила не по формату")
        return Verdict(verdict, str(data.get("reason") or "")[:300])

    async def photo(self, jpeg, part):
        text = PHOTO_PROMPT.format(manufacturer=part["manufacturer"], model=part["model"])
        return await self._ask([{"type": "text", "text": text}, image_part(jpeg)], fallback=UNSURE)

    async def manual(self, text, part):
        prompt = MANUAL_PROMPT.format(manufacturer=part["manufacturer"], model=part["model"], text=text[:4000])
        return await self._ask([{"type": "text", "text": prompt}], fallback=MATCH)

    async def close(self):
        await self.llm.close()


class NoVerifier:
    """Проверка выключена: достаточно автоматических проверок (код модели в PDF, размер картинки)."""

    async def photo(self, jpeg, part):
        return Verdict(MATCH)

    async def manual(self, text, part):
        return Verdict(MATCH)

    async def close(self):
        pass
