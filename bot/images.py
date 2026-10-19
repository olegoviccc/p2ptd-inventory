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


def _fit(draw, text, max_width, size, min_size=18):
    """Самый крупный шрифт (не больше size), при котором текст влезает в ширину."""
    while size > min_size and draw.textlength(text, font=_font(size)) > max_width:
        size -= 2
    return _font(size)


def _wrap(draw, text, font, max_width):
    """Перенос по словам с учётом реальной ширины букв; слишком длинное слово режется."""
    lines, cur = [], ""
    for word in text.split():
        candidate = f"{cur} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width:
            cur = candidate
            continue
        if cur:
            lines.append(cur)
        while draw.textlength(word, font=font) > max_width and len(word) > 1:
            cut = len(word)
            while cut > 1 and draw.textlength(word[:cut], font=font) > max_width:
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]
        cur = word
    if cur:
        lines.append(cur)
    return lines


def label_png(stock_pk, number, title, subtitle=""):
    """Этикетка 62×29 мм при 300 dpi: QR позиции InvenTree + номер и модель.

    Номер и подпись уменьшаются, пока не влезут: ничего не обрезается.
    """
    w, h, pad = 732, 342, 28  # справа запас: принтеры часто не печатают край
    img = Image.new("RGB", (w, h), "white")
    qr = qrcode.make(json.dumps({"stockitem": stock_pk}), border=1, box_size=10)
    img.paste(qr.resize((h - 20, h - 20)).convert("RGB"), (10, 10))

    draw = ImageDraw.Draw(img)
    x = h + 4
    max_width = w - x - pad
    draw.text((x, 30), number, font=_fit(draw, number, max_width, 64), fill="black")
    title_font = _font(32)
    for i, line in enumerate(_wrap(draw, title, title_font, max_width)[:3]):
        draw.text((x, 120 + i * 42), line, font=title_font, fill="black")
    if subtitle:
        draw.text((x, h - 48), subtitle, font=_fit(draw, subtitle, max_width, 26, 14), fill="black")

    out = io.BytesIO()
    img.save(out, "PNG", dpi=(300, 300))
    return out.getvalue()
