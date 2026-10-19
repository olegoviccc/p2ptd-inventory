"""Логика бота без Telegram: распознавание → черновик → сохранение в InvenTree."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

from . import images
from .inventree import InvenTreeError
from .matching import best_match
from .recognize import UNKNOWN_CATEGORY

log = logging.getLogger(__name__)

# Номера выдаются под одной блокировкой: два сотрудника одновременно не получат один номер.
_numbering_lock = asyncio.Lock()


@dataclass
class Photo:
    name: str
    raw: bytes      # оригинал, уходит во вложения InvenTree
    jpeg: bytes     # уменьшенная копия для модели
    barcodes: list


async def load_photo(name, raw):
    jpeg, barcodes = await asyncio.to_thread(images.analyze, raw)
    return Photo(name, raw, jpeg, barcodes)


@dataclass
class Draft:
    location_pk: int
    location_label: str
    manufacturer: str = ""
    model: str = ""
    serial: str = ""
    serial_confirmed: bool = False  # серийник совпал со штрихкодом
    category_pk: int | None = None
    category_label: str = ""
    part_pk: int | None = None
    part_name: str = ""
    rider_stock_pk: int | None = None
    rider_qty: float = 0
    duplicate: str = ""  # номер уже заведённой позиции с тем же заводским S/N
    overview_photo: int = -1
    confidence: str = ""
    notes: str = ""

    @property
    def name(self):
        return self.part_name or " ".join(p for p in (self.manufacturer, self.model) if p)

    @property
    def can_save(self):
        return bool(self.model) and not self.duplicate and bool(self.part_pk or self.category_pk)


@dataclass
class SaveResult:
    stock_pk: int
    number: str
    part_name: str
    created_part: bool
    warnings: list = field(default_factory=list)


async def recognize(it, catalog, recognizer, photos, location_pk, location_label):
    await catalog.load(it)
    barcodes = [b for p in photos for b in p.barcodes]
    rec = await recognizer.recognize([p.jpeg for p in photos], sorted(catalog.category_paths.values()),
                                     catalog.model_names, barcodes)
    draft = Draft(location_pk=location_pk, location_label=location_label, manufacturer=rec.manufacturer,
                  model=rec.model, serial=rec.serial, overview_photo=rec.overview_photo,
                  confidence=rec.confidence, notes=rec.notes)
    if rec.category != UNKNOWN_CATEGORY:
        draft.category_pk = next((pk for pk, p in catalog.category_paths.items() if p == rec.category), None)
        draft.category_label = rec.category if draft.category_pk else ""
    await refresh(it, catalog, draft, barcodes)
    return draft, rec


async def _none():
    return []


async def refresh(it, catalog, draft, barcodes=()):
    """Пересчитывает совпадение с каталогом, позицию из райдера и дубликат (и после правки)."""
    await catalog.load(it)
    draft.serial_confirmed = bool(draft.serial) and any(
        draft.serial.replace(" ", "") in b.replace(" ", "") for b in barcodes)

    match = best_match(draft.manufacturer, draft.model, catalog.parts, catalog.mpns)
    draft.part_pk = match.part["pk"] if match else None
    draft.part_name = match.part["name"] if match else ""
    if match and match.part.get("category"):
        draft.category_pk = match.part["category"]
        draft.category_label = catalog.category_paths.get(draft.category_pk, "")

    draft.rider_stock_pk, draft.rider_qty, draft.duplicate = None, 0, ""
    if draft.part_pk:
        unverified, same = await asyncio.gather(
            it.unverified_stock(draft.part_pk, draft.location_pk),
            it.find_by_batch(draft.serial, draft.part_pk) if draft.serial else _none(),
        )
        if unverified:
            draft.rider_stock_pk = unverified[0]["pk"]
            draft.rider_qty = float(unverified[0]["quantity"])
        if same:
            draft.duplicate = same[0].get("serial") or f"#{same[0]['pk']}"
    return draft


async def next_number(it, numbering, resync=False):
    """Следующий номер. С InvenTree сверяемся при первом вызове и после конфликта."""
    if numbering.last is None or resync:
        serials = [i.get("serial") for i in await it.search_stock(numbering.prefix, detail=False)]
        numbering.last = max(numbering.last or 0, numbering.highest(serials))
    return numbering.format(numbering.last + 1)


async def _assign_number(it, numbering, stock_pk, batch):
    resync = False
    for _ in range(5):
        candidate = await next_number(it, numbering, resync)
        try:
            await it.set_serial(stock_pk, candidate, batch=batch)
        except InvenTreeError as e:
            if e.status != 400 or "serial" not in e.detail.lower():
                raise
            log.warning("номер %s уже занят, сверяюсь с InvenTree", candidate)
            numbering.last = numbering.parse(candidate)
            resync = True
            continue
        numbering.last = numbering.parse(candidate)
        return candidate
    raise ValueError("не удалось выдать внутренний номер, попробуйте ещё раз")


async def save(it, catalog, numbering, draft, photos, author):
    """Позиция с внутренним номером + списание 1 шт. из райдера + фото."""
    if draft.duplicate:
        raise ValueError(f"этот серийник уже заведён: {draft.duplicate}")

    created = False
    if draft.part_pk:
        part = await it.part(draft.part_pk)
    elif draft.category_pk:
        part = await it.create_part(draft.name, draft.category_pk, draft.manufacturer, draft.model)
        catalog.invalidate()
        created = True
    else:
        raise ValueError("не выбрана категория для новой модели")
    await it.ensure_trackable(part)

    notes = f"Добавлено через Telegram-бота: {author}."
    if draft.notes:
        notes += f"\n\nРаспознавание: {draft.notes}"

    async with _numbering_lock:
        stock = await it.create_stock(part["pk"], draft.location_pk, notes)
        try:
            number = await _assign_number(it, numbering, stock["pk"], draft.serial or None)
        except Exception:
            await it.delete_stock(stock["pk"])
            raise

    # Остальное независимо друг от друга — параллельно
    tasks, labels = [], []
    if draft.rider_stock_pk:
        tasks.append(it.take_one(draft.rider_stock_pk, f"Сверено при обходе: {number}"))
        labels.append("списание 1 шт. из позиции райдера")
    for i, p in enumerate(photos, 1):
        tasks.append(it.attach("stockitem", stock["pk"], p.name, p.raw, comment=f"Фото {i} при добавлении"))
        labels.append(f"фото {i}")
    if 0 <= draft.overview_photo < len(photos) and not part.get("image"):
        p = photos[draft.overview_photo]
        tasks.append(it.set_part_image(part["pk"], p.name, p.raw))
        labels.append("картинка модели")

    result = SaveResult(stock["pk"], number, part["name"], created)
    for label, outcome in zip(labels, await asyncio.gather(*tasks, return_exceptions=True)):
        if isinstance(outcome, Exception):
            detail = outcome.detail if isinstance(outcome, InvenTreeError) else str(outcome)
            log.warning("%s: %s", label, detail)
            result.warnings.append(f"{label}: не получилось ({detail[:100]})")
    return result


async def lookup(it, numbering, text):
    """Позиции по внутреннему номеру, заводскому серийнику или произвольному поиску."""
    text = text.strip()
    if numbering.is_internal(text):
        item = await it.find_by_serial(numbering.normalize(text))
        if item:
            return [item]
        # номер мог остаться в старом формате (P2PTD-00042): ищем по цифрам и сверяем число
        n = numbering.parse(text)
        found = await it.search_stock(f"{n:0{numbering.digits}d}")
        return [i for i in found if numbering.parse(i.get("serial")) == n]
    by_batch, by_serial = await asyncio.gather(it.find_by_batch(text), it.find_by_serial(text))
    if by_batch or by_serial:
        return by_batch or [by_serial]
    return (await it.search_stock(text))[:10]


# --- правка существующих позиций


async def move(it, pk, location_pk, author):
    item = await it.stock_item(pk)
    if item.get("location") == location_pk:
        return item
    await it.transfer(pk, item["quantity"], location_pk, f"Перемещено через Telegram-бота: {author}")
    return await it.stock_item(pk)


async def set_status(it, pk, status):
    await it.update_stock(pk, status=status)
    return await it.stock_item(pk)


async def set_batch(it, pk, batch):
    """Заводской S/N (поле Batch). Тот же S/N у другой позиции этой модели не допускается."""
    batch = batch.strip()
    item = await it.stock_item(pk)
    if batch:
        same = [i for i in await it.find_by_batch(batch, item["part"]) if i["pk"] != pk]
        if same:
            raise ValueError(f"этот S/N уже у позиции {same[0].get('serial') or '#' + str(same[0]['pk'])}")
    await it.update_stock(pk, batch=batch)
    return await it.stock_item(pk)


async def add_note(it, pk, text, author):
    """Дописывает заметку с датой и автором, не затирая прежние."""
    item = await it.stock_item(pk)
    if "notes" not in item:
        raise ValueError("InvenTree не отдал текущие заметки; чтобы их не затереть, заметка не сохранена")
    line = f"{datetime.now():%d.%m.%Y %H:%M} {author}: {text.strip()}"
    await it.update_stock(pk, notes="\n\n".join(p for p in ((item.get("notes") or "").strip(), line) if p))
    return await it.stock_item(pk)


async def add_photo(it, pk, photo, author):
    await it.attach("stockitem", pk, photo.name, photo.raw, comment=f"Добавлено через бота: {author}")


# --- мануалы деталей

MANUAL_WORDS = ("руководство", "мануал", "manual", "инструкция")


def is_manual_attachment(attachment):
    """Вложение похоже на мануал: PDF-файл или ссылка, либо подпись со словом «руководство»."""
    name = (attachment.get("filename") or attachment.get("attachment") or attachment.get("link") or "").lower()
    comment = (attachment.get("comment") or "").lower()
    return name.split("?")[0].endswith(".pdf") or any(w in comment for w in MANUAL_WORDS)


async def part_manuals(it, part_pk):
    return [a for a in await it.part_attachments(part_pk) if is_manual_attachment(a)]
