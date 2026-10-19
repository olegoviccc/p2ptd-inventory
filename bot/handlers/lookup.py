"""Поиск позиции по номеру P2PTD, заводскому серийнику или фото QR-этикетки."""

import asyncio

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from .. import images, service, ui
from . import Add, Find

router = Router(name="lookup")

free_text = F.text & ~F.text.in_(ui.MENU_TEXTS) & ~F.text.startswith("/")


@router.message(Command("find"))
@router.message(F.text == ui.FIND)
async def find_start(message: Message, state: FSMContext):
    await state.set_state(Find.query)
    await message.answer("Пришлите номер P2PTD, заводской серийник или фото QR-этикетки.",
                         reply_markup=ui.cancel_kb())


@router.message(StateFilter(None, Find.query, Add.photos), free_text)
async def find_text(message: Message, state: FSMContext, user, it, numbering, store):
    items = await service.lookup(it, numbering, message.text)
    store.log_event(message.from_user.id, user.username, "lookup", query=message.text[:50], found=len(items))
    if await state.get_state() == Find.query.state:
        await state.clear()
    if not items:
        return await message.answer(f"Ничего не нашёл. Добавить прибор: «{ui.ADD}».")
    for item in items[:5]:
        await message.answer(ui.item_text(item, it.web_url("stock/item", item["pk"])))
    if len(items) > 5:
        await message.answer(f"…и ещё {len(items) - 5}. Уточните запрос.")


@router.message(StateFilter(None, Find.query), F.photo | F.document)
async def find_photo(message: Message, state: FSMContext, bot: Bot, it):
    file = message.photo[-1].file_id if message.photo else message.document.file_id
    raw = (await bot.download(file)).read()
    codes = await asyncio.to_thread(images.read_barcodes, raw)
    pks = [pk for pk in map(images.stockitem_from_qr, codes) if pk]
    await state.clear()
    if not pks:
        return await message.answer(f"QR-этикетки на фото не нашёл. Чтобы добавить прибор, нажмите «{ui.ADD}».")
    item = await it.stock_item(pks[0])
    await message.answer(ui.item_text(item, it.web_url("stock/item", item["pk"])))
