"""Логика бота без Telegram: черновик позиции по распознаванию и сохранение в InvenTree."""

import asyncio
from dataclasses import dataclass, field

from .inventree import InvenTreeError
from .matching import best_match
from .recognize import UNKNOWN_CATEGORY

# Номера выдаются под одной блокировкой: два сотрудника одновременно не получат один номер.
_numbering_lock = asyncio.Lock()


def tree_paths(nodes):
    """{pk: "Звук / Микрофоны / Проводные"} для категорий или мест."""
    by_pk = {n["pk"]: n for n in nodes}
    paths = {}

    def path(pk):
        if pk not in paths:
            node = by_pk[pk]
            parent = node.get("parent")
            paths[pk] = f"{path(parent)} / {node['name']}" if parent in by_pk else node["name"]
        return paths[pk]

    for pk in by_pk:
        path(pk)
    return paths


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
    duplicate: str = ""  # внутренний номер уже заведённой позиции с тем же S/N
    overview_photo: int = -1
    confidence: str = ""
    notes: str = ""

    @property
    def name(self):
        return self.part_name or " ".join(p for p in (self.manufacturer, self.model) if p)


@dataclass
class SaveResult:
    stock_pk: int
    number: str
    part_name: str
    warnings: list = field(default_factory=list)


async def category_paths(it):
    return tree_paths(await it.categories())


async def known_model_names(it):
    return sorted(p["name"] for p in await it.parts())


async def build_draft(it, rec, location_pk, location_label, barcodes):
    """Черновик позиции из распознавания: сопоставление с каталогом и складом."""
    cats = await category_paths(it)
    draft = Draft(
        location_pk=location_pk,
        location_label=location_label,
        manufacturer=rec.manufacturer.strip(),
        model=rec.model.strip(),
        serial=rec.serial.strip(),
        overview_photo=rec.overview_photo,
        confidence=rec.confidence,
        notes=rec.notes,
    )
    if rec.category != UNKNOWN_CATEGORY:
        draft.category_pk = next((pk for pk, path in cats.items() if path == rec.category), None)
        draft.category_label = rec.category if draft.category_pk else ""
    await refresh(it, draft, barcodes, cats)
    return draft


async def refresh(it, draft, barcodes=(), cats=None):
    """Пересчитывает совпадение с каталогом, позицию из райдера и дубликат (после правки тоже)."""
    cats = cats or await category_paths(it)
    draft.serial_confirmed = bool(draft.serial) and any(
        draft.serial.replace(" ", "") in b.replace(" ", "") for b in barcodes
    )

    match = best_match(draft.manufacturer, draft.model, await it.parts(), await it.manufacturer_parts())
    draft.part_pk = match.part["pk"] if match else None
    draft.part_name = match.part["name"] if match else ""
    if match and match.part.get("category"):
        draft.category_pk = match.part["category"]
        draft.category_label = cats.get(draft.category_pk, "")

    draft.rider_stock_pk, draft.rider_qty, draft.duplicate = None, 0, ""
    if draft.part_pk:
        unverified = await it.unverified_stock(draft.part_pk, draft.location_pk)
        if unverified:
            draft.rider_stock_pk = unverified[0]["pk"]
            draft.rider_qty = float(unverified[0]["quantity"])
        if draft.serial:
            same = await it.find_by_batch(draft.serial, draft.part_pk)
            if same:
                draft.duplicate = same[0].get("serial") or f"#{same[0]['pk']}"
    return draft


async def next_number(it, numbering):
    serials = [i.get("serial") for i in await it.search_stock(numbering.prefix)]
    return numbering.next_after(serials)


async def save(it, numbering, draft, photos, author):
    """Создаёт позицию с внутренним номером, списывает 1 шт. из райдера, прикладывает фото.

    photos: список (имя файла, bytes).
    """
    if draft.duplicate:
        raise ValueError(f"этот серийник уже заведён: {draft.duplicate}")

    if draft.part_pk:
        part = await it.get(f"/api/part/{draft.part_pk}/")
    else:
        if not draft.category_pk:
            raise ValueError("не выбрана категория для новой модели")
        part = await it.create_part(draft.name, draft.category_pk, draft.manufacturer, draft.model)
    await it.ensure_trackable(part)

    notes = f"Добавлено через Telegram-бота: {author}."
    if draft.notes:
        notes += f"\n\nРаспознавание: {draft.notes}"

    async with _numbering_lock:
        stock = await it.create_stock(part["pk"], draft.location_pk, notes)
        number = None
        for _ in range(5):
            candidate = await next_number(it, numbering)
            try:
                await it.set_serial(stock["pk"], candidate, batch=draft.serial or None)
                number = candidate
                break
            except InvenTreeError as e:
                if e.status != 400 or "serial" not in e.detail.lower():
                    await it.delete_stock(stock["pk"])
                    raise
        if number is None:
            await it.delete_stock(stock["pk"])
            raise ValueError("не удалось выдать внутренний номер, попробуйте ещё раз")

    result = SaveResult(stock["pk"], number, part["name"])

    if draft.rider_stock_pk:
        try:
            await it.take_one(draft.rider_stock_pk, f"Сверено при обходе: {number}")
        except InvenTreeError as e:
            result.warnings.append(f"не удалось списать 1 шт. из позиции райдера: {e.detail}")

    for i, (filename, data) in enumerate(photos):
        try:
            await it.attach("stockitem", stock["pk"], filename, data, comment=f"Фото {i + 1} при добавлении")
        except InvenTreeError as e:
            result.warnings.append(f"фото {i + 1} не приложено: {e.detail}")

    if 0 <= draft.overview_photo < len(photos) and not part.get("image"):
        filename, data = photos[draft.overview_photo]
        try:
            await it.set_part_image(part["pk"], filename, data)
        except InvenTreeError as e:
            result.warnings.append(f"картинка модели не установлена: {e.detail}")

    return result


async def lookup(it, numbering, text):
    """Позиции по внутреннему номеру, заводскому серийнику или произвольному поиску."""
    text = text.strip()
    if numbering.is_internal(text):
        item = await it.find_by_serial(text.upper())
        return [item] if item else []
    items = await it.find_by_batch(text)
    if items:
        return items
    item = await it.find_by_serial(text)
    if item:
        return [item]
    return (await it.search_stock(text))[:10]
