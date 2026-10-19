"""Карточка позиции и её правка: место, статус, заводской S/N, заметка, фото, этикетка, удаление."""

import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from .. import service, ui
from ..inventree import InvenTreeError
from . import ItemEdit

log = logging.getLogger(__name__)
router = Router(name="item")

free_text = F.text & ~F.text.in_(ui.MENU_TEXTS) & ~F.text.startswith("/")


async def show(target, it, item, edit=False):
    """Карточка позиции с кнопками: новым сообщением или вместо текущего."""
    text, markup = ui.item_text(item, it.web_url("stock/item", item["pk"])), ui.item_kb(item)
    if edit:
        await target.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


def is_item(action):
    return ui.Item.filter(F.action == action)


@router.callback_query(is_item("open"))
async def open_item(call: CallbackQuery, callback_data: ui.Item, state: FSMContext, it):
    await state.clear()
    item = await it.stock_item(callback_data.pk)
    # карточка из подписи к этикетке — новым сообщением, иначе на месте текущего
    await show(call.message, it, item, edit=bool(call.message.text))
    await call.answer()


# --- перемещение


@router.callback_query(is_item("move"))
async def move_start(call: CallbackQuery, callback_data: ui.Item, it, catalog):
    await catalog.load(it)
    await call.message.edit_text("Куда переместить?", reply_markup=ui.move_venues_kb(catalog.locations, callback_data.pk))
    await call.answer()


@router.callback_query(is_item("mv_venue"))
async def move_venue(call: CallbackQuery, callback_data: ui.Item, it, catalog):
    await catalog.load(it)
    await call.message.edit_text("Какое место?", reply_markup=ui.move_places_kb(
        catalog.locations, callback_data.pk, callback_data.value))
    await call.answer()


@router.callback_query(is_item("mv_set"))
async def move_set(call: CallbackQuery, callback_data: ui.Item, it, catalog, user, store):
    item = await service.move(it, callback_data.pk, callback_data.value, user.username)
    place = catalog.location_paths.get(callback_data.value, str(callback_data.value))
    log.info("перемещено %s → %s", item.get("serial") or item["pk"], place)
    store.log_event(call.from_user.id, user.username, "moved", number=item.get("serial"), place=place)
    await show(call.message, it, item, edit=True)
    await call.answer(f"Перемещено: {place}")


# --- статус


@router.callback_query(is_item("status"))
async def status_start(call: CallbackQuery, callback_data: ui.Item, it):
    item = await it.stock_item(callback_data.pk)
    await call.message.edit_text("Новый статус:", reply_markup=ui.status_kb(item["pk"], item.get("status")))
    await call.answer()


@router.callback_query(is_item("st_set"))
async def status_set(call: CallbackQuery, callback_data: ui.Item, it, user, store):
    item = await service.set_status(it, callback_data.pk, callback_data.value)
    name = ui.STATUS_NAMES.get(callback_data.value, callback_data.value)
    store.log_event(call.from_user.id, user.username, "status", number=item.get("serial"), status=name)
    await show(call.message, it, item, edit=True)
    await call.answer(f"Статус: {name}")


# --- текстовые правки: заводской S/N и заметка


PROMPTS = {
    "batch": "Пришлите заводской серийник. Чтобы очистить поле, пришлите «-».",
    "note": "Пришлите текст заметки. Она добавится к прежним с датой и вашим именем.",
}


@router.callback_query(ui.Item.filter(F.action.in_(list(PROMPTS))))
async def text_edit_start(call: CallbackQuery, callback_data: ui.Item, state: FSMContext):
    await state.set_state(ItemEdit.batch if callback_data.action == "batch" else ItemEdit.note)
    await state.update_data(item_pk=callback_data.pk)
    await call.message.answer(PROMPTS[callback_data.action], reply_markup=ui.item_input_kb(callback_data.pk))
    await call.answer()


@router.message(ItemEdit.batch, free_text)
async def batch_set(message: Message, state: FSMContext, it, user, store):
    pk = (await state.get_data())["item_pk"]
    value = "" if message.text.strip() == "-" else message.text
    try:
        item = await service.set_batch(it, pk, value)
    except ValueError as e:
        return await message.answer(f"Не сохранено: {ui.esc(e)}", reply_markup=ui.item_input_kb(pk))
    await state.clear()
    store.log_event(message.from_user.id, user.username, "batch", number=item.get("serial"), serial=value.strip())
    await show(message, it, item)


@router.message(ItemEdit.note, free_text)
async def note_add(message: Message, state: FSMContext, it, user, store):
    pk = (await state.get_data())["item_pk"]
    try:
        item = await service.add_note(it, pk, message.text, user.username)
    except ValueError as e:
        return await message.answer(f"Не сохранено: {ui.esc(e)}", reply_markup=ui.item_input_kb(pk))
    await state.clear()
    store.log_event(message.from_user.id, user.username, "note", number=item.get("serial"), text=message.text[:80])
    await show(message, it, item)


# --- фото


@router.callback_query(is_item("photo"))
async def photo_start(call: CallbackQuery, callback_data: ui.Item, state: FSMContext):
    await state.set_state(ItemEdit.photo)
    await state.update_data(item_pk=callback_data.pk, photo_count=0)
    await call.message.answer("Пришлите фото — каждое сразу приложится к позиции.",
                              reply_markup=ui.buttons(("✅ Готово", ui.Item(action="open", pk=callback_data.pk))))
    await call.answer()


@router.message(ItemEdit.photo, F.photo | F.document)
async def photo_add(message: Message, state: FSMContext, bot: Bot, it, user, store):
    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    name = (message.document.file_name if message.document else None) or f"photo_{message.message_id}.jpg"
    raw = (await bot.download(file_id)).read()
    data = await state.get_data()
    await service.add_photo(it, data["item_pk"], service.Photo(name, raw, raw, []), user.username)
    store.log_event(message.from_user.id, user.username, "photo", item=data["item_pk"])
    await message.answer("📷 Приложено.", reply_markup=ui.buttons(("✅ Готово", ui.Item(action="open", pk=data["item_pk"]))))


# --- этикетка


@router.callback_query(is_item("label"))
async def label(call: CallbackQuery, callback_data: ui.Item, it, labels):
    item = await it.stock_item(callback_data.pk)
    await call.answer("Готовлю этикетку…")
    part = item.get("part_detail") or {}
    place = (item.get("location_detail") or {}).get("pathstring", "")
    filename, data = await labels.make(it, item["pk"], item["serial"], part.get("full_name") or part.get("name", ""), place)
    await call.message.answer_document(BufferedInputFile(data, filename), caption=item["serial"])


# --- мануал модели


@router.callback_query(is_item("manual"))
async def manual(call: CallbackQuery, callback_data: ui.Item, it, scraper=None):
    item = await it.stock_item(callback_data.pk)
    manuals = await service.part_manuals(it, item["part"])
    await call.answer()
    if not manuals:
        rows = [("🔎 Найти мануал и фото сейчас", ui.Item(action="scrape", pk=item["pk"]))] if scraper else []
        return await call.message.answer("📘 Мануала у этой модели пока нет. Его ищет ночной поиск каталога.",
                                         reply_markup=ui.buttons(*rows, ui.back_to_item(item["pk"])))
    for a in manuals[:3]:
        if a.get("link") and not a.get("attachment"):
            await call.message.answer(f"📘 {ui.esc(a.get('comment') or 'Руководство')}\n{a['link']}")
            continue
        data = await it.fetch_bytes(a["attachment"])
        name = a.get("filename") or a["attachment"].rsplit("/", 1)[-1] or "manual.pdf"
        await call.message.answer_document(BufferedInputFile(data, name), caption="📘 " + (a.get("comment") or "")[:200])


@router.callback_query(is_item("scrape"))
async def scrape_now(call: CallbackQuery, callback_data: ui.Item, bot: Bot, it, user, scraper=None):
    if scraper is None:
        return await call.answer("Поиск мануалов не настроен", show_alert=True)
    if scraper.running:
        return await call.answer("Сейчас идёт поиск по каталогу, попробуйте позже", show_alert=True)
    item = await it.stock_item(callback_data.pk)
    from .catalog import in_background
    in_background(scrape_part(bot, call.message.chat.id, scraper, item, user.username))
    await call.answer("Ищу, пришлю результат")


async def scrape_part(bot, chat_id, scraper, item, author):
    from scraper.engine import Busy
    name = (item.get("part_detail") or {}).get("full_name") or f"#{item['part']}"
    try:
        report = await scraper.run(f"бот: {author}", part_pk=item["part"], force=True)
    except Busy as e:
        return await bot.send_message(chat_id, f"Не запущено: {e}.")
    except Exception as e:  # noqa: BLE001 — фоновая задача: сообщаем, а не роняем бота
        log.exception("поиск для одной модели упал")
        return await bot.send_message(chat_id, f"⚠️ Поиск прервался: {ui.esc(e)}")
    if report.no_model and not report.total:
        return await bot.send_message(chat_id, f"У «{ui.esc(name)}» нет производителя и кода модели — искать нечего.")
    found = [w for w, items in (("мануал", report.manuals), ("фото", report.photos)) if items]
    lines = [f"🔎 <b>{ui.esc(name)}</b>"]
    if found:
        lines.append("Нашлось и приложено: " + ", ".join(found))
    if report.review:
        lines.append("На проверке у администратора: " + ", ".join(w for _, w in report.review))
    if report.not_found:
        lines.append("Не нашлось: " + ", ".join(w for _, w in report.not_found))
    if report.total == 0:
        lines.append("Мануал и фото у модели уже есть.")
    await bot.send_message(chat_id, "\n".join(lines), reply_markup=ui.buttons(ui.back_to_item(item["pk"])))


# --- удаление


@router.callback_query(is_item("delete"))
async def delete_ask(call: CallbackQuery, callback_data: ui.Item, it):
    item = await it.stock_item(callback_data.pk)
    await call.message.edit_text(
        f"Удалить позицию <b>{ui.esc(item.get('serial') or '#' + str(item['pk']))}</b> из InvenTree?\n"
        "Это нужно только для ошибочно заведённых. Списанный прибор лучше пометить статусом.",
        reply_markup=ui.delete_kb(item["pk"]))
    await call.answer()


@router.callback_query(is_item("del_yes"))
async def delete_yes(call: CallbackQuery, callback_data: ui.Item, it, user, store):
    item = await it.stock_item(callback_data.pk)
    try:
        await it.delete_stock(item["pk"])
    except InvenTreeError as e:
        if e.status != 403:
            raise
        await call.answer("Нет прав на удаление (Stock Items: Delete). Поставьте статус «Уничтожено».", show_alert=True)
        return
    number = item.get("serial") or f"#{item['pk']}"
    log.info("удалено %s", number)
    store.log_event(call.from_user.id, user.username, "deleted", number=number)
    await call.message.edit_text(f"🗑 Позиция {ui.esc(number)} удалена.")
    await call.answer()
