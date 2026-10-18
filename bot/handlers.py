"""Telegram-обработчики: вход, добавление по фото, поиск."""

import asyncio
import html
import logging
from collections import defaultdict
from dataclasses import asdict

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from . import images, service
from .inventree import InvenTree, InvenTreeError
from .recognize import RecognitionError
from .service import Draft

log = logging.getLogger(__name__)
router = Router()
_photo_locks = defaultdict(asyncio.Lock)

STATUS_NAMES = {10: "OK", 50: "Требует внимания", 55: "Повреждено", 60: "Уничтожено",
                65: "Отклонено", 70: "Потеряно", 75: "Карантин", 85: "Возвращено"}
CONFIDENCE = {"high": "высокая", "medium": "средняя", "low": "низкая"}
EDIT_KEYS = {
    "производитель": "manufacturer", "manufacturer": "manufacturer",
    "модель": "model", "model": "model",
    "s/n": "serial", "sn": "serial", "серийник": "serial", "serial": "serial",
}
MAX_PHOTOS = 5


class Login(StatesGroup):
    username = State()
    password = State()


class Add(StatesGroup):
    venue = State()
    photos = State()
    confirm = State()
    edit = State()
    category = State()


def esc(text):
    return html.escape(str(text or ""))


def client_for(user_id, store, cfg):
    user = store.get(user_id)
    return InvenTree(cfg.inventree_url, token=user.token) if user else None


async def need_login(message):
    await message.answer("Сначала войдите своим аккаунтом InvenTree: /login")


# --- общие команды


@router.message(CommandStart())
@router.message(Command("help"))
async def start(message: Message, store, **_):
    user = store.get(message.from_user.id)
    who = f"Вы вошли как <b>{esc(user.username)}</b>.\n\n" if user else "Сначала войдите: /login\n\n"
    await message.answer(
        who
        + "/add — добавить прибор по фото\n"
        + "Пришлите номер P2PTD-…, заводской серийник или фото QR-этикетки — покажу позицию.\n"
        + "/cancel — отменить текущее действие\n"
        + "/logout — выйти"
    )


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext, **_):
    await state.clear()
    await message.answer("Отменено.")


# --- вход


@router.message(Command("login"))
async def login_start(message: Message, state: FSMContext, **_):
    await state.set_state(Login.username)
    await message.answer("Логин в InvenTree:")


@router.message(Login.username, F.text)
async def login_username(message: Message, state: FSMContext, **_):
    await state.update_data(username=message.text.strip())
    await state.set_state(Login.password)
    await message.answer("Пароль (сообщение с паролем я сразу удалю):")


@router.message(Login.password, F.text)
async def login_password(message: Message, state: FSMContext, store, cfg, **_):
    password = message.text
    try:
        await message.delete()
    except Exception:  # noqa: BLE001 — в некоторых чатах удалить нельзя, это не критично
        log.warning("не удалось удалить сообщение с паролем")
    username = (await state.get_data())["username"]
    await state.clear()
    try:
        token = await InvenTree(cfg.inventree_url, basic=(username, password)).obtain_token()
        it = InvenTree(cfg.inventree_url, token=token)
        roles = await it.roles()
    except InvenTreeError as e:
        text = "Неверный логин или пароль." if e.status in (400, 401, 403) else f"Ошибка InvenTree: {esc(e.detail)}"
        await message.answer(text + " Попробуйте ещё раз: /login")
        return
    store.save(message.from_user.id, username, token)
    missing = missing_roles(roles)
    text = f"Готово, вы вошли как <b>{esc(username)}</b>. Добавить прибор: /add"
    if missing:
        text += "\n\n⚠️ У вашей группы не хватает прав, часть действий не сработает:\n" + "\n".join(missing)
    await message.answer(text)


REQUIRED_ROLES = {
    "part": ["view", "add", "change"],
    "part_category": ["view"],
    "stock": ["view", "add", "change"],
    "stock_location": ["view"],
    "purchase_order": ["view", "add"],
}


def missing_roles(info):
    if info.get("is_superuser"):
        return []
    roles = info.get("roles") or {}
    return [f"• {role}: {perm}" for role, perms in REQUIRED_ROLES.items()
            for perm in perms if perm not in (roles.get(role) or [])]


@router.message(Command("logout"))
async def logout(message: Message, state: FSMContext, store, **_):
    store.delete(message.from_user.id)
    await state.clear()
    await message.answer("Вы вышли. Токен в InvenTree можно отозвать в настройках аккаунта.")


# --- выбор места


@router.message(Command("add"))
async def add_start(message: Message, state: FSMContext, store, cfg, **_):
    it = client_for(message.from_user.id, store, cfg)
    if not it:
        return await need_login(message)
    await state.clear()
    await state.set_state(Add.venue)
    await show_venues(message, it, store.get(message.from_user.id))


async def show_venues(message, it, user):
    try:
        locations = await it.locations()
    except InvenTreeError as e:
        return await message.answer(f"Ошибка InvenTree: {esc(e.detail)}")
    kb = InlineKeyboardBuilder()
    if user.location_pk:
        kb.button(text=f"📍 Как в прошлый раз: {user.location_label}", callback_data=f"place:{user.location_pk}")
    for loc in sorted((l for l in locations if not l.get("parent")), key=lambda l: l["name"]):
        kb.button(text=loc["name"], callback_data=f"venue:{loc['pk']}")
    kb.adjust(1, 2)
    await message.answer("Где вы?", reply_markup=kb.as_markup())


@router.callback_query(Add.venue, F.data.startswith("venue:"))
async def pick_venue(call: CallbackQuery, store, cfg, **_):
    it = client_for(call.from_user.id, store, cfg)
    venue_pk = int(call.data.split(":")[1])
    children = [l for l in await it.locations() if l.get("parent") == venue_pk]
    kb = InlineKeyboardBuilder()
    for loc in sorted(children, key=lambda l: l["name"]):
        kb.button(text=loc["name"], callback_data=f"place:{loc['pk']}")
    kb.button(text="Само заведение", callback_data=f"place:{venue_pk}")
    kb.adjust(2)
    await call.message.edit_text("Какое место?", reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(Add.venue, F.data.startswith("place:"))
async def pick_place(call: CallbackQuery, state: FSMContext, store, cfg, **_):
    it = client_for(call.from_user.id, store, cfg)
    pk = int(call.data.split(":")[1])
    paths = service.tree_paths(await it.locations())
    label = paths.get(pk, str(pk))
    store.set_location(call.from_user.id, pk, label)
    await state.update_data(location_pk=pk, location_label=label, photos=[], status_msg=None)
    await state.set_state(Add.photos)
    await call.message.edit_text(
        f"📍 <b>{esc(label)}</b>\n\n"
        "Пришлите 1–3 фото прибора: шильдик крупно и общий вид. "
        "Если отправить фото файлом, мелкий текст читается лучше."
    )
    await call.answer()


# --- фото и распознавание


def photo_file(message):
    if message.photo:
        return message.photo[-1].file_id, "photo.jpg"
    if message.document and (message.document.mime_type or "").startswith("image/"):
        return message.document.file_id, message.document.file_name or "photo.jpg"
    return None


@router.message(StateFilter(Add.photos, Add.confirm), F.photo | F.document)
async def add_photo(message: Message, state: FSMContext, bot: Bot, **_):
    file = photo_file(message)
    if not file:
        return await message.answer("Это не изображение. Пришлите фото.")
    async with _photo_locks[message.from_user.id]:
        if await state.get_state() == Add.confirm.state:
            await state.update_data(photos=[], status_msg=None, draft=None)  # новые фото = новый прибор
            await state.set_state(Add.photos)
        data = await state.get_data()
        photos = data.get("photos", [])
        if len(photos) >= MAX_PHOTOS:
            return
        photos.append(list(file))
        kb = InlineKeyboardBuilder()
        kb.button(text=f"🔍 Распознать ({len(photos)} фото)", callback_data="recognize")
        kb.button(text="📍 Сменить место", callback_data="change_place")
        kb.adjust(1)
        text = f"Фото: {len(photos)}. Можно добавить ещё или распознать."
        if data.get("status_msg"):
            try:
                await bot.delete_message(message.chat.id, data["status_msg"])
            except Exception:  # noqa: BLE001 — старое сообщение могло исчезнуть
                pass
        status = await message.answer(text, reply_markup=kb.as_markup())
        await state.update_data(photos=photos, status_msg=status.message_id)


@router.callback_query(StateFilter(Add.photos, Add.confirm), F.data == "change_place")
async def change_place(call: CallbackQuery, state: FSMContext, store, cfg, **_):
    it = client_for(call.from_user.id, store, cfg)
    await state.set_state(Add.venue)
    await call.answer()
    await show_venues(call.message, it, store.get(call.from_user.id))


async def download(bot, photos):
    out = []
    for file_id, name in photos:
        buf = await bot.download(file_id)
        out.append((name, buf.read()))
    return out


@router.callback_query(Add.photos, F.data == "recognize")
async def recognize(call: CallbackQuery, state: FSMContext, bot: Bot, store, cfg, recognizer, **_):
    it = client_for(call.from_user.id, store, cfg)
    if not it:
        return await need_login(call.message)
    data = await state.get_data()
    if not data.get("photos"):
        return await call.answer("Сначала пришлите фото")
    await call.answer()
    await call.message.edit_text("🔍 Распознаю…")

    try:
        files = await download(bot, data["photos"])
        barcodes = [b for _, raw in files for b in images.read_barcodes(raw)]
        prepared = [images.prepare(raw) for _, raw in files]
        cats = await service.category_paths(it)
        known = await service.known_model_names(it)
        rec = await recognizer.recognize(prepared, sorted(cats.values()), known, barcodes)
        draft = await service.build_draft(it, rec, data["location_pk"], data["location_label"], barcodes)
    except RecognitionError as e:
        return await call.message.edit_text(f"Не получилось распознать: {esc(e)}. Попробуйте другое фото.")
    except InvenTreeError as e:
        return await call.message.edit_text(f"Ошибка InvenTree: {esc(e.detail)}")

    await state.update_data(draft=asdict(draft), barcodes=barcodes, status_msg=None)
    await state.set_state(Add.confirm)
    await call.message.edit_text(card_text(draft), reply_markup=card_kb(draft))


def card_text(d: Draft):
    lines = [f"<b>{esc(d.name) or 'Модель не распознана'}</b>"]
    if d.serial:
        mark = "✓ совпал со штрихкодом" if d.serial_confirmed else "⚠️ сверьте с фото"
        lines.append(f"S/N: <code>{esc(d.serial)}</code> {mark}")
    else:
        lines.append("S/N: не найден (если он есть, нажмите «Исправить»)")
    lines.append(f"Категория: {esc(d.category_label) or 'не выбрана'}")
    lines.append(f"Место: {esc(d.location_label)}")
    lines.append("В каталоге: есть" if d.part_pk else "В каталоге: нет, модель будет создана")
    if d.rider_stock_pk:
        lines.append(f"Из райдера: здесь {d.rider_qty:g} шт. не проверено, одна будет сверена")
    if d.duplicate:
        lines.append(f"\n⛔ Прибор с этим S/N уже заведён: <b>{esc(d.duplicate)}</b>")
    if d.confidence:
        lines.append(f"\nУверенность: {CONFIDENCE.get(d.confidence, d.confidence)}")
    if d.notes:
        lines.append(f"Заметка: {esc(d.notes)}")
    return "\n".join(lines)


def card_kb(d: Draft):
    kb = InlineKeyboardBuilder()
    if not d.duplicate and (d.part_pk or d.category_pk) and d.model:
        kb.button(text="✅ Сохранить", callback_data="save")
    kb.button(text="✏️ Исправить", callback_data="edit")
    if not d.part_pk:
        kb.button(text="🗂 Категория", callback_data="cat:root")
    kb.button(text="✖️ Отмена", callback_data="drop")
    kb.adjust(1)
    return kb.as_markup()


async def load_draft(state):
    data = await state.get_data()
    return Draft(**data["draft"]), data


@router.callback_query(Add.confirm, F.data == "save")
async def save(call: CallbackQuery, state: FSMContext, bot: Bot, store, cfg, numbering, **_):
    it = client_for(call.from_user.id, store, cfg)
    draft, data = await load_draft(state)
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    try:
        files = await download(bot, data["photos"])
        author = store.get(call.from_user.id).username
        result = await service.save(it, numbering, draft, files, author)
    except (InvenTreeError, ValueError) as e:
        detail = e.detail if isinstance(e, InvenTreeError) else str(e)
        await call.message.answer(f"Не сохранено: {esc(detail)}", reply_markup=card_kb(draft))
        return

    label = images.label_png(result.stock_pk, result.number, result.part_name, draft.location_label)
    caption = (f"✅ <b>{result.number}</b> — {esc(result.part_name)}\n{esc(draft.location_label)}\n"
               f"{it.web_url('stock/item', result.stock_pk)}")
    if result.warnings:
        caption += "\n\n⚠️ " + "\n⚠️ ".join(esc(w) for w in result.warnings)
    await call.message.answer_document(BufferedInputFile(label, f"{result.number}.png"), caption=caption)
    await state.update_data(photos=[], draft=None, status_msg=None)
    await state.set_state(Add.photos)
    await call.message.answer("Следующий прибор? Присылайте фото. Сменить место: /add")


@router.callback_query(Add.confirm, F.data == "drop")
async def drop(call: CallbackQuery, state: FSMContext, **_):
    await state.update_data(photos=[], draft=None, status_msg=None)
    await state.set_state(Add.photos)
    await call.message.edit_text("Отменено. Присылайте фото следующего прибора.")
    await call.answer()


# --- правка


@router.callback_query(Add.confirm, F.data == "edit")
async def edit_start(call: CallbackQuery, state: FSMContext, **_):
    draft, _data = await load_draft(state)
    await state.set_state(Add.edit)
    template = f"Производитель: {draft.manufacturer}\nМодель: {draft.model}\nS/N: {draft.serial}"
    await call.message.answer(
        "Скопируйте, исправьте и отправьте обратно:\n\n<code>" + esc(template) + "</code>"
    )
    await call.answer()


def parse_edit(text):
    values = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        field = EDIT_KEYS.get(key.strip().casefold())
        if field:
            values[field] = value.strip()
    return values


@router.message(Add.edit, F.text)
async def edit_apply(message: Message, state: FSMContext, store, cfg, **_):
    values = parse_edit(message.text)
    if not values:
        return await message.answer("Не нашёл строк вида «Модель: …». Попробуйте ещё раз.")
    it = client_for(message.from_user.id, store, cfg)
    draft, data = await load_draft(state)
    for key, value in values.items():
        setattr(draft, key, value)
    draft.notes = ""
    try:
        await service.refresh(it, draft, data.get("barcodes", []))
    except InvenTreeError as e:
        return await message.answer(f"Ошибка InvenTree: {esc(e.detail)}")
    await state.update_data(draft=asdict(draft))
    await state.set_state(Add.confirm)
    await message.answer(card_text(draft), reply_markup=card_kb(draft))


# --- выбор категории для новой модели


@router.callback_query(Add.confirm, F.data.startswith("cat:"))
async def pick_category(call: CallbackQuery, state: FSMContext, store, cfg, **_):
    it = client_for(call.from_user.id, store, cfg)
    cats = await it.categories()
    paths = service.tree_paths(cats)
    arg = call.data.split(":", 1)[1]

    if arg.startswith("set"):
        pk = int(arg[3:])
        draft, _data = await load_draft(state)
        draft.category_pk, draft.category_label = pk, paths[pk]
        await state.update_data(draft=asdict(draft))
        await call.message.edit_text(card_text(draft), reply_markup=card_kb(draft))
        return await call.answer()

    parent = None if arg == "root" else int(arg)
    kb = InlineKeyboardBuilder()
    if parent is not None:
        kb.button(text=f"✅ {paths[parent]}", callback_data=f"cat:set{parent}")
    for c in sorted((c for c in cats if c.get("parent") == parent), key=lambda c: c["name"]):
        has_children = any(x.get("parent") == c["pk"] for x in cats)
        target = f"cat:{c['pk']}" if has_children else f"cat:set{c['pk']}"
        kb.button(text=c["name"] + (" ›" if has_children else ""), callback_data=target)
    kb.adjust(1)
    await call.message.edit_text("Категория:", reply_markup=kb.as_markup())
    await call.answer()


# --- поиск (вне сценария добавления)


def item_card(it, item):
    part = (item.get("part_detail") or {}).get("full_name") or (item.get("part_detail") or {}).get("name", "")
    loc = (item.get("location_detail") or {}).get("pathstring", "—")
    lines = [f"<b>{esc(item.get('serial') or '#' + str(item['pk']))}</b> — {esc(part)}"]
    if item.get("batch"):
        lines.append(f"Заводской S/N: <code>{esc(item['batch'])}</code>")
    if not item.get("serial"):
        lines.append(f"Количество: {float(item.get('quantity', 0)):g}")
    lines.append(f"Место: {esc(loc)}")
    lines.append(f"Статус: {STATUS_NAMES.get(item.get('status'), item.get('status'))}")
    lines.append(it.web_url("stock/item", item["pk"]))
    return "\n".join(lines)


@router.message(StateFilter(None), F.photo | F.document)
async def lookup_photo(message: Message, bot: Bot, store, cfg, **_):
    it = client_for(message.from_user.id, store, cfg)
    if not it:
        return await need_login(message)
    file = photo_file(message)
    if not file:
        return
    raw = (await bot.download(file[0])).read()
    pks = [pk for pk in (images.stockitem_from_qr(t) for t in images.read_barcodes(raw)) if pk]
    if not pks:
        return await message.answer("QR-этикетки на фото не нашёл. Чтобы добавить прибор: /add")
    try:
        await message.answer(item_card(it, await it.stock_item(pks[0])))
    except InvenTreeError as e:
        await message.answer(f"Ошибка InvenTree: {esc(e.detail)}")


@router.message(StateFilter(None, Add.photos), F.text & ~F.text.startswith("/"))
async def lookup_text(message: Message, store, cfg, numbering, **_):
    it = client_for(message.from_user.id, store, cfg)
    if not it:
        return await need_login(message)
    try:
        items = await service.lookup(it, numbering, message.text)
    except InvenTreeError as e:
        return await message.answer(f"Ошибка InvenTree: {esc(e.detail)}")
    if not items:
        return await message.answer("Ничего не нашёл. Чтобы добавить прибор: /add")
    for item in items[:5]:
        await message.answer(item_card(it, item))
    if len(items) > 5:
        await message.answer(f"…и ещё {len(items) - 5}. Уточните запрос.")
