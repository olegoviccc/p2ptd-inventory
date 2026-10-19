"""Вход своим аккаунтом InvenTree."""

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from .. import ui
from ..inventree import InvenTreeError
from . import Login

log = logging.getLogger(__name__)
router = Router(name="auth")

REQUIRED_ROLES = {
    "part": ["view", "add", "change"],
    "part_category": ["view"],
    "stock": ["view", "add", "change"],
    "stock_location": ["view"],
    "purchase_order": ["view", "add"],
}

free_text = F.text & ~F.text.in_(ui.MENU_TEXTS) & ~F.text.startswith("/")


def missing_roles(info):
    if info.get("is_superuser"):
        return []
    roles = info.get("roles") or {}
    return [f"• {role}: {perm}" for role, perms in REQUIRED_ROLES.items()
            for perm in perms if perm not in (roles.get(role) or [])]


@router.message(Command("login"))
@router.message(F.text == ui.LOGIN)
async def login_start(message: Message, state: FSMContext):
    await state.set_state(Login.username)
    await message.answer("Логин в InvenTree:", reply_markup=ui.cancel_kb())


@router.message(Login.username, free_text)
async def login_username(message: Message, state: FSMContext):
    await state.update_data(username=message.text.strip())
    await state.set_state(Login.password)
    await message.answer("Пароль (сообщение с паролем я сразу удалю):", reply_markup=ui.cancel_kb())


@router.message(Login.password, F.text & ~F.text.in_(ui.MENU_TEXTS))
async def login_password(message: Message, state: FSMContext, store, api, is_admin):
    password = message.text
    try:
        await message.delete()
    except Exception:  # noqa: BLE001 — в некоторых чатах удалять нельзя, это не критично
        log.warning("не удалось удалить сообщение с паролем")
    username = (await state.get_data())["username"]
    await state.clear()
    try:
        token = await api.basic(username, password).obtain_token()
        roles = await api.client(token).roles()
    except InvenTreeError as e:
        log.info("вход не удался для %s: %s", username, e.status)
        text = "Неверный логин или пароль." if e.status in (400, 401, 403) else \
            f"InvenTree недоступен: {ui.esc(e.detail[:200])}"
        return await message.answer(text, reply_markup=ui.main_menu(False))

    store.save(message.from_user.id, username, token)
    store.log_event(message.from_user.id, username, "login")
    text = f"Готово, вы вошли как <b>{ui.esc(username)}</b>."
    missing = missing_roles(roles)
    if missing:
        text += "\n\n⚠️ У вашей группы не хватает прав, часть действий не сработает:\n" + "\n".join(missing)
    await message.answer(text, reply_markup=ui.main_menu(True, is_admin))
