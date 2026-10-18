"""Распознавание оборудования по фото через Claude (vision + structured outputs)."""

import base64
import json
from dataclasses import dataclass

import anthropic

UNKNOWN_CATEGORY = "Не определено"

PROMPT = """You are cataloguing professional sound, light, video, DJ and IT equipment for a venue inventory.
The photos show one device: usually its nameplate/label and sometimes an overall view.

Extract:
- manufacturer: brand as printed (e.g. "Pioneer DJ", "Yamaha", "Behringer"); "" if unknown.
- model: exact model code as printed on the device (e.g. "XDJ-1000MK2"), not a series name; "" if unknown.
- serial: the manufacturer's serial number (S/N, Serial No., SN). Copy it character by character.
  Never guess or complete digits. If it is not clearly readable, return "".
- category: the best matching category from the allowed list.
- overview_photo: 0-based index of the photo that shows the whole device best (for the catalogue picture), -1 if none.
- confidence: "high" if manufacturer, model and serial are all clearly readable; "medium" if something is
  inferred from the device's appearance; "low" if the photos are unclear.
- notes: short note in Russian about anything the person should check (e.g. "серийник частично закрыт").

{known}Barcodes decoded from the photos (exact, use them to confirm the serial): {barcodes}"""


@dataclass
class Recognition:
    manufacturer: str
    model: str
    serial: str
    category: str
    overview_photo: int
    confidence: str
    notes: str

    @property
    def name(self):
        return " ".join(p for p in (self.manufacturer, self.model) if p)


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
        "required": ["manufacturer", "model", "serial", "category", "overview_photo", "confidence", "notes"],
        "additionalProperties": False,
    }


class RecognitionError(Exception):
    pass


class Recognizer:
    def __init__(self, model, client=None):
        self.model = model
        self.client = client or anthropic.AsyncAnthropic()

    async def recognize(self, photos, categories, known_models=(), barcodes=()):
        """photos: список JPEG (bytes), categories: пути категорий «Звук / Микрофоны / Проводные»."""
        known = ""
        if known_models:
            known = "Models already in the inventory (prefer these exact spellings if it is one of them):\n"
            known += "\n".join(f"- {m}" for m in known_models) + "\n\n"
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.standard_b64encode(p).decode()}}
            for p in photos
        ]
        content.append({"type": "text", "text": PROMPT.format(
            known=known, barcodes=", ".join(barcodes) or "none")})

        try:
            response = await self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"format": {"type": "json_schema", "schema": schema(categories)}},
                messages=[{"role": "user", "content": content}],
            )
        except anthropic.APIConnectionError as e:
            raise RecognitionError("нет связи с Claude API") from e
        except anthropic.APIStatusError as e:
            raise RecognitionError(f"Claude API ответил {e.status_code}") from e

        if response.stop_reason == "refusal":
            raise RecognitionError("модель отказалась обрабатывать фото")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            raise RecognitionError(f"пустой ответ ({response.stop_reason})")
        data = json.loads(text)
        result = Recognition(**{k: data[k] for k in Recognition.__dataclass_fields__})
        if not 0 <= result.overview_photo < len(photos):
            result.overview_photo = -1
        return result
