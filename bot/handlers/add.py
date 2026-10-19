"""Добавление прибора: место → фото → распознавание → карточка → сохранение."""

import logging
from dataclasses import asdict

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from .. import images, service, ui
from ..recognize import RecognitionError
from ..service import Draft
from . import Add

log = logging.getLogger(__name__)
router = Router(name="add")

free_text = F.text & ~F.text.in_(ui.MENU_TEXTS) & ~F.text.startswith("/")
EDIT_KEYS = {
    "производитель": "manufacturer", "manufacturer": "manufacturer",
    "модель": "model", "model": "model",
    "s/n": "serial", "sn": "serial", "серийник": "serial", "serial": "serial",
}


def photos_prompt(label):
    return (f"📍 <b>{ui.esc(label)}</b>\n\nПришлите 1–3 фото прибора: шильдик крупно и общий вид. "
            "Если отправить фото файлом, мелкий текст читается лучше.")


async def draft_of(state):
    data = await state.get_data()
    return Draft(**data["draft"]), data


# --- старт и выбор места


@router.message(Command("add"))
@router.message(F.text == ui.ADD)
async def add_start(message: Message, state: FSMContext, user, it, catalog, photos):
    photos.clear(message.from_user.id)
    if user.location_pk:  # место уже известно — сразу к фото
        await state.set_state(Add.photos)
        await state.update_data(location_pk=user.location_pk, location_label=user.location_label)
        return await message.answer(photos_prompt(user.location_label),
                                    reply_markup=ui.buttons(("📍 Сменить место", ui.CHANGE_PLACE)))
    await ask_venue(message, state, user, it, catalog)


@router.message(Command("place"))
@router.message(F.text == ui.PLACE)
async def place_cmd(message: Message, state: FSMContext, user, it, catalog):
    await ask_venue(message, state, user, it, catalog)


async def ask_venue(message, state, user, it, catalog, edit=False):
    await catalog.load(it)
    await state.set_state(Add.venue)
    markup = ui.venues_kb(catalog.locations, user.location_pk, user.location_label)
    if edit:
        await message.edit_text("Где вы?", reply_markup=markup)
    else:
        await message.answer("Где вы?", reply_markup=markup)


@router.callback_query(F.data == ui.CHANGE_PLACE)
async def change_place(call: CallbackQuery, state: FSMContext, user, it, catalog):
    await ask_venue(call.message, state, user, it, catalog, edit=True)
    await call.answer()


@router.callback_query(Add.venue, ui.Loc.filter(F.action == "venue"))
async def pick_venue(call: CallbackQuery, callback_data: ui.Loc, it, catalog):
    await catalog.load(it)
    await call.message.edit_text("Какое место?", reply_markup=ui.places_kb(catalog.locations, callback_data.pk))
    await call.answer()


@router.callback_query(Add.venue, ui.Loc.filter(F.action == "set"))
async def pick_place(call: CallbackQuery, callback_data: ui.Loc, state: FSMContext, store, it, catalog):
    await catalog.load(it)
    label = catalog.location_paths.get(callback_data.pk, str(callback_data.pk))
    store.set_location(call.from_user.id, callback_data.pk, label)
    await state.set_state(Add.photos)
    await state.update_data(location_pk=callback_data.pk, location_label=label)
    await call.message.edit_text(photos_prompt(label))
    await call.answer()


# --- фото


def photo_file(message):
    if message.photo:
        return message.photo[-1].file_id, f"photo_{message.message_id}.jpg"
    if message.document and (message.document.mime_type or "").startswith("image/"):
        return message.document.file_id, message.document.file_name or f"photo_{message.message_id}.jpg"
    return None


@router.message(StateFilter(Add.photos, Add.confirm), F.photo | F.document)
async def add_photo(message: Message, state: FSMContext, bot: Bot, photos):
    file = photo_file(message)
    if not file:
        return await message.answer("Это не изображение. Пришлите фото.")
    uid = message.from_user.id
    if await state.get_state() == Add.confirm.state:  # новые фото после карточки = следующий прибор
        photos.clear(uid)
        await state.set_state(Add.photos)

    # Скачивание и обработка — вне блокировки, чтобы фото из альбома шли параллельно
    raw = (await bot.download(file[0])).read()
    photo = await service.load_photo(file[1], raw)

    async with photos.lock(uid):
        if not photos.add(uid, photo):
            return
        data = await state.get_data()
        if data.get("status_msg"):
            try:
                await bot.delete_message(message.chat.id, data["status_msg"])
            except Exception:  # noqa: BLE001 — старое сообщение могло уже исчезнуть
                pass
        count = len(photos.get(uid))
        found = f" Штрихкодов на фото: {len(photo.barcodes)}." if photo.barcodes else ""
        status = await message.answer(f"Фото: {count}.{found} Можно добавить ещё или распознать.",
                                      reply_markup=ui.photos_kb(count))
        await state.update_data(status_msg=status.message_id)


# --- распознавание и карточка


@router.callback_query(Add.photos, F.data == ui.RECOGNIZE)
async def recognize(call: CallbackQuery, state: FSMContext, user, it, catalog, recognizer, photos, store):
    batch = photos.get(call.from_user.id)
    if not batch:
        return await call.answer("Сначала пришлите фото", show_alert=True)
    await call.answer()
    await call.message.edit_text("🔍 Распознаю…")
    data = await state.get_data()
    try:
        draft, rec = await service.recognize(it, catalog, recognizer, batch,
                                             data["location_pk"], data["location_label"])
    except RecognitionError as e:
        store.log_event(call.from_user.id, user.username, "recognize_failed", error=str(e)[:200])
        return await call.message.edit_text(f"Не получилось распознать: {ui.esc(e)}\nПопробуйте другое фото.",
                                            reply_markup=ui.photos_kb(len(batch)))
    log.info("распознано %s за %d мс (%s, %s)", rec.name, rec.elapsed_ms, rec.engine, rec.confidence)
    store.log_event(call.from_user.id, user.username, "recognized", model=draft.name, serial=draft.serial,
                    engine=rec.engine, ms=rec.elapsed_ms, confidence=rec.confidence)
    await state.update_data(draft=asdict(draft), barcodes=[b for p in batch for b in p.barcodes], status_msg=None)
    await state.set_state(Add.confirm)
    await call.message.edit_text(ui.card_text(draft), reply_markup=ui.card_kb(draft))


@router.callback_query(Add.confirm, F.data == ui.SAVE)
async def save(call: CallbackQuery, state: FSMContext, user, it, catalog, numbering, photos, store):
    draft, _ = await draft_of(state)
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    batch = photos.get(call.from_user.id)
    try:
        result = await service.save(it, catalog, numbering, draft, batch, user.username)
    except ValueError as e:
        store.log_event(call.from_user.id, user.username, "save_failed", model=draft.name, error=str(e))
        return await call.message.answer(f"Не сохранено: {ui.esc(e)}", reply_markup=ui.card_kb(draft))

    log.info("сохранено %s: %s в %s", result.number, result.part_name, draft.location_label)
    store.log_event(call.from_user.id, user.username, "saved", number=result.number, model=result.part_name,
                    serial=draft.serial, place=draft.location_label, new_model=result.created_part or None)
    label = images.label_png(result.stock_pk, result.number, result.part_name, draft.location_label)
    caption = (f"✅ <b>{result.number}</b> — {ui.esc(result.part_name)}\n{ui.esc(draft.location_label)}\n"
               f"{it.web_url('stock/item', result.stock_pk)}")
    if result.warnings:
        caption += "\n\n⚠️ " + "\n⚠️ ".join(ui.esc(w) for w in result.warnings)
    await call.message.answer_document(BufferedInputFile(label, f"{result.number}.png"), caption=caption,
                                       reply_markup=ui.buttons(("✏️ Изменить", ui.Item(action="open", pk=result.stock_pk))))

    photos.clear(call.from_user.id)
    await state.set_state(Add.photos)
    await state.update_data(draft=None, status_msg=None)
    await call.message.answer("Следующий прибор? Присылайте фото.",
                              reply_markup=ui.buttons(("📍 Сменить место", ui.CHANGE_PLACE)))


@router.callback_query(Add.confirm, F.data == ui.DROP)
async def drop(call: CallbackQuery, state: FSMContext, photos):
    photos.clear(call.from_user.id)
    await state.set_state(Add.photos)
    await state.update_data(draft=None, status_msg=None)
    await call.message.edit_text("Отменено. Присылайте фото следующего прибора.")
    await call.answer()


# --- правка


@router.callback_query(Add.confirm, F.data == ui.EDIT)
async def edit_start(call: CallbackQuery, state: FSMContext):
    draft, _ = await draft_of(state)
    await state.set_state(Add.edit)
    template = f"Производитель: {draft.manufacturer}\nМодель: {draft.model}\nS/N: {draft.serial}"
    await call.message.answer("Скопируйте, исправьте и отправьте обратно:\n\n<code>" + ui.esc(template) + "</code>",
                              reply_markup=ui.buttons(("⬅️ Без изменений", ui.EDIT)))
    await call.answer()


@router.callback_query(Add.edit, F.data == ui.EDIT)
async def edit_back(call: CallbackQuery, state: FSMContext):
    draft, _ = await draft_of(state)
    await state.set_state(Add.confirm)
    await call.message.edit_text(ui.card_text(draft), reply_markup=ui.card_kb(draft))
    await call.answer()


def parse_edit(text):
    values = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        field = EDIT_KEYS.get(key.strip().casefold()) if sep else None
        if field:
            values[field] = value.strip()
    return values


@router.message(Add.edit, free_text)
async def edit_apply(message: Message, state: FSMContext, it, catalog):
    values = parse_edit(message.text)
    if not values:
        return await message.answer("Не нашёл строк вида «Модель: …». Попробуйте ещё раз.")
    draft, data = await draft_of(state)
    for key, value in values.items():
        setattr(draft, key, value)
    draft.notes = ""
    await service.refresh(it, catalog, draft, data.get("barcodes", []))
    await state.update_data(draft=asdict(draft))
    await state.set_state(Add.confirm)
    await message.answer(ui.card_text(draft), reply_markup=ui.card_kb(draft))


# --- категория для новой модели


@router.callback_query(Add.confirm, F.data == ui.CATEGORY)
async def category_start(call: CallbackQuery, it, catalog):
    await catalog.load(it)
    await call.message.edit_text("Категория:", reply_markup=ui.categories_kb(catalog.categories,
                                                                              catalog.category_paths, 0))
    await call.answer()


@router.callback_query(Add.confirm, ui.Cat.filter())
async def category_pick(call: CallbackQuery, callback_data: ui.Cat, state: FSMContext, it, catalog):
    await catalog.load(it)
    if callback_data.action == "open":
        await call.message.edit_text("Категория:", reply_markup=ui.categories_kb(
            catalog.categories, catalog.category_paths, callback_data.pk))
        return await call.answer()
    draft, _ = await draft_of(state)
    if callback_data.action == "set":
        draft.category_pk = callback_data.pk
        draft.category_label = catalog.category_paths.get(callback_data.pk, "")
        await state.update_data(draft=asdict(draft))
    await call.message.edit_text(ui.card_text(draft), reply_markup=ui.card_kb(draft))
    await call.answer()
