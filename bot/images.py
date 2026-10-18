"""Фото: подготовка к распознаванию, штрихкоды, этикетки."""

import io
import json
from functools import lru_cache
from pathlib import Path

import qrcode
import zxingcpp
from PIL import Image, ImageDraw, ImageFont, ImageOps

MAX_SIDE = 1568  # больше модели не нужно, лишние пиксели только замедляют и стоят денег


def _open(data):
    return ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")


def analyze(data):
    """Одно открытие фото: (JPEG для модели, штрихкоды). Блокирующая, вызывать через to_thread."""
    img = _open(data)
    barcodes = [r.text for r in zxingcpp.read_barcodes(img) if r.text]
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue(), barcodes


def read_barcodes(data):
    return [r.text for r in zxingcpp.read_barcodes(_open(data)) if r.text]


def stockitem_from_qr(text):
    """pk позиции из QR InvenTree ({"stockitem": 42}), иначе None."""
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return None
    if isinstance(value, dict) and isinstance(value.get("stockitem"), int):
        return value["stockitem"]
    return None


FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
]


@lru_cache(maxsize=8)
def _font(size):
    for p in FONT_PATHS:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size)


def label_png(stock_pk, number, title, subtitle=""):
    """Этикетка 62×29 мм при 300 dpi: QR позиции InvenTree + номер и модель."""
    w, h = 732, 342
    img = Image.new("RGB", (w, h), "white")
    qr = qrcode.make(json.dumps({"stockitem": stock_pk}), border=1, box_size=10)
    img.paste(qr.resize((h - 20, h - 20)).convert("RGB"), (10, 10))

    draw = ImageDraw.Draw(img)
    x = h + 10
    draw.text((x, 40), number, font=_font(56), fill="black")
    for i, line in enumerate(_wrap(title, 22)[:3]):
        draw.text((x, 130 + i * 44), line, font=_font(34), fill="black")
    if subtitle:
        draw.text((x, h - 50), subtitle[:30], font=_font(26), fill="black")

    out = io.BytesIO()
    img.save(out, "PNG", dpi=(300, 300))
    return out.getvalue()


def _wrap(text, width):
    lines, cur = [], ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > width:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        lines.append(cur)
    return lines
