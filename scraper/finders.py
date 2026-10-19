"""Поиск мануала (PDF) и фото модели. Ничего не пишет в InvenTree — только находит и проверяет."""

import io
import logging
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps
from pypdf import PdfReader

from bot.matching import norm

from .search import SearchError
from .verify import MATCH, MISMATCH, NoVerifier
from .web import FetchError, page_image

log = logging.getLogger(__name__)

MIN_PDF_BYTES = 30_000
MIN_IMAGE_SIDE = 300
MAX_IMAGE_SIDE = 1600


@dataclass
class Found:
    data: bytes
    url: str | None   # откуда взято (None — локальный файл)
    source: str       # olegovic | manufacturer | retailer | search
    filename: str
    status: str = "ok"  # ok — прикладываем; review — модель не уверена, решает человек
    reason: str = ""    # пояснение модели


class Pick:
    """Перебор кандидатов: первый подтверждённый — сразу; первый сомнительный — на проверку человеку."""

    def __init__(self, verifier):
        self.verifier = verifier or NoVerifier()
        self.review = None

    def judge(self, found, verdict):
        """Found для возврата или None, если искать дальше."""
        found.reason = verdict.reason
        if verdict.verdict == MATCH:
            return found
        if verdict.verdict == MISMATCH:
            log.info("модель отклонила %s: %s", found.url or found.filename, verdict.reason)
        elif self.review is None:
            found.status = "review"
            self.review = found
        return None


# --- проверки


def pdf_text(data, pages=3):
    """Текст первых страниц настоящего PDF или None."""
    if not data.startswith(b"%PDF") or len(data) < MIN_PDF_BYTES:
        return None
    try:
        reader = PdfReader(io.BytesIO(data))
        return " ".join((reader.pages[i].extract_text() or "") for i in range(min(pages, len(reader.pages))))
    except Exception:  # noqa: BLE001 — битые PDF встречаются часто, это просто «не подходит»
        return None


def pdf_names_model(data, model, pages=3):
    """Настоящий PDF, и на первых страницах есть код модели."""
    text = pdf_text(data, pages)
    return bool(text) and norm(model) in norm(text)


def normalize_image(data):
    """JPEG не больше 1600 px; None — не картинка или слишком маленькая."""
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
        img.load()
    except Exception:  # noqa: BLE001 — не картинка
        return None
    if min(img.size) < MIN_IMAGE_SIDE:
        return None
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        background = Image.new("RGB", img.size, "white")
        background.paste(img, mask=img.split()[-1])
        img = background
    img = img.convert("RGB")
    img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90)
    return out.getvalue()


def mentions(model, *texts):
    key = norm(model)
    return any(key in norm(t) for t in texts if t)


def safe_name(text):
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in text).strip("_") or "file"


# --- мануал


def olegovic_manual(folder, manufacturer, model):
    """PDF, который olegovic-llm уже нашёл и проверил: <папка>/<Производитель>/<MPN>.pdf."""
    if not folder or not Path(folder).is_dir():
        return None
    key, brand = norm(model), norm(manufacturer)
    for pdf in Path(folder).glob("*/*.pdf"):
        if norm(pdf.stem) == key and (not brand or brand in norm(pdf.parent.name) or norm(pdf.parent.name) in brand):
            return pdf
    return None


async def find_manual(part, search, web, sources, olegovic_folder=None, use_search=True,
                      verifier=None, rejected=frozenset()):
    mfr, model = part["manufacturer"], part["model"]
    pick = Pick(verifier)
    filename = f"{safe_name(model)}.pdf"

    async def check(data, url, source):
        text = pdf_text(data)
        if not text or norm(model) not in norm(text):
            return None
        return pick.judge(Found(data, url, source, filename), await pick.verifier.manual(text, part))

    local = olegovic_manual(olegovic_folder, mfr, model)
    if local and str(local) not in rejected:
        found = await check(local.read_bytes(), None, "olegovic")
        if found:
            return found
    if not use_search:
        return pick.review

    queries = [f"{mfr} {model} manual pdf"] + [f"{model} manual site:{d}" for d in sources.domains(mfr)[:2]]
    seen, results = set(rejected), []
    for query in queries:
        try:
            for r in await search.text(query, 10):
                if r["url"] and r["url"] not in seen and not sources.is_skipped(r["url"]):
                    seen.add(r["url"])
                    results.append(r)
        except SearchError as e:
            log.warning("поиск «%s»: %s", query, e)

    def score(r):
        url = r["url"].lower().split("?")[0]
        return (not sources.is_manufacturer(r["url"], mfr), not url.endswith(".pdf"),
                not mentions(model, r["url"], r["title"]), "manual" not in (url + r["title"].lower()))

    for r in sorted(results, key=score)[:6]:
        try:
            data, _ = await web.get(r["url"])
        except FetchError as e:
            log.info("мануал %s: %s", model, e)
            continue
        source = "manufacturer" if sources.is_manufacturer(r["url"], mfr) else "search"
        found = await check(data, r["url"], source)
        if found:
            return found
    return pick.review


# --- фото


async def _image_at(web, url):
    try:
        data, _ = await web.get(url, max_bytes=15 * 1024 * 1024)
    except FetchError as e:
        log.info("картинка %s: %s", url, e)
        return None
    return normalize_image(data)


async def find_photo(part, search, web, sources, verifier=None, rejected=frozenset()):
    mfr, model = part["manufacturer"], part["model"]
    pick = Pick(verifier)
    name = f"{safe_name(model)}.jpg"

    async def check(image_url, page_url, source):
        if image_url in rejected or page_url in rejected:
            return None
        jpeg = await _image_at(web, image_url)
        if not jpeg:
            return None
        return pick.judge(Found(jpeg, page_url, source, name), await pick.verifier.photo(jpeg, part))

    # 1. Страница модели на сайте производителя → og:image
    pages = []
    for query in [f"{mfr} {model}"] + [f"{model} site:{d}" for d in sources.domains(mfr)[:1]]:
        try:
            pages += [r for r in await search.text(query, 10)
                      if sources.is_manufacturer(r["url"], mfr) and mentions(model, r["url"], r["title"])]
        except SearchError as e:
            log.warning("поиск «%s»: %s", query, e)
    for r in list({r["url"]: r for r in pages}.values())[:3]:
        if r["url"] in rejected:
            continue
        try:
            html, ctype = await web.get(r["url"], max_bytes=3 * 1024 * 1024)
        except FetchError as e:
            log.info("страница %s: %s", r["url"], e)
            continue
        image_url = page_image(html.decode("utf-8", "replace"), r["url"]) if "html" in ctype or not ctype else None
        found = image_url and await check(image_url, r["url"], "manufacturer")
        if found:
            return found

    # 2. Поиск картинок: производитель, затем магазины, с кодом модели в подписи
    try:
        images = await search.images(f"{mfr} {model}", 25)
    except SearchError as e:
        log.warning("поиск картинок %s: %s", model, e)
        return pick.review
    candidates = [i for i in images if i["image"] and not sources.is_skipped(i["url"] or i["image"])
                  and mentions(model, i["title"], i["url"], i["image"])
                  and (sources.is_manufacturer(i["url"] or i["image"], mfr) or sources.is_retailer(i["url"] or i["image"]))]
    candidates.sort(key=lambda i: (not sources.is_manufacturer(i["url"] or i["image"], mfr),
                                   -(i["width"] or 0) * (i["height"] or 0)))
    for i in candidates[:5]:
        page = i["url"] or i["image"]
        source = "manufacturer" if sources.is_manufacturer(page, mfr) else "retailer"
        found = await check(i["image"], page, source)
        if found:
            return found
    return pick.review
