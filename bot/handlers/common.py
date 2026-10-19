"""Старт, помощь, профиль, выход, отмена, журнал, обработка ошибок."""

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ErrorEvent, Message

from .. import ui
from ..inventree import InvenTreeError

log = logging.getLogger(__name__)
router = Router(name="common")


@router.message(CommandStart())
@router.message(Command("help"))
@router.message(F.text == ui.HELP)
async def help_(message: Message, user, is_admin):
    greeting = f"Вы вошли как <b>{ui.esc(user.username)}</b>.\n\n" if user else \
        f"Чтобы начать, нажмите «{ui.LOGIN}» и войдите своим аккаунтом InvenTree.\n\n"
    await message.answer(greeting + ui.HELP_TEXT, reply_markup=ui.main_menu(bool(user), is_admin))


@router.message(Command("cancel"))
async def cancel_cmd(message: Message, state: FSMContext, photos, user, is_admin):
    await state.clear()
    photos.clear(message.from_user.id)
    await message.answer("Отменено.", reply_markup=ui.main_menu(bool(user), is_admin))


@router.callback_query(F.data == ui.CANCEL)
async def cancel_btn(call: CallbackQuery, state: FSMContext, photos):
    await state.clear()
    photos.clear(call.from_user.id)
    await call.message.edit_text("Отменено.")
    await call.answer()


@router.message(Command("profile"))
@router.message(F.text == ui.PROFILE)
async def profile(message: Message, user, cfg):
    if not user:
        return await message.answer("Вы не вошли.", reply_markup=ui.main_menu(False))
    await message.answer(
        f"👤 <b>{ui.esc(user.username)}</b>\n"
        f"📍 {ui.esc(user.location_label) or 'место не выбрано'}\n"
        f"InvenTree: {cfg.inventree_url}",
        reply_markup=ui.buttons(("🚪 Выйти", ui.LOGOUT)),
    )


@router.callback_query(F.data == ui.LOGOUT)
async def logout(call: CallbackQuery, state: FSMContext, store, photos, user):
    store.delete(call.from_user.id)
    store.log_event(call.from_user.id, user.username if user else "", "logout")
    await state.clear()
    photos.clear(call.from_user.id)
    await call.message.edit_text("Вы вышли. Токен бота можно отозвать в настройках аккаунта InvenTree.")
    await call.message.answer("До встречи!", reply_markup=ui.main_menu(False))
    await call.answer()


@router.message(Command("log"))
@router.message(F.text == ui.JOURNAL)
async def journal(message: Message, store, is_admin):
    if not is_admin:
        return await message.answer("Журнал доступен только администраторам.")
    await message.answer("📜 <b>Последние действия</b>\n\n" + ui.journal_text(store.recent_events(25)))


async def on_error(event: ErrorEvent, store, cfg, bot):
    """Ошибки из любых обработчиков: понятный ответ пользователю, лог, уведомление администраторам."""
    update, exc = event.update, event.exception
    source = update.message or update.callback_query
    tg_user = source.from_user if source else None
    origin = update.callback_query.message if update.callback_query else update.message

    if isinstance(exc, InvenTreeError) and exc.status == 401 and tg_user:
        store.delete(tg_user.id)
        text = "Сессия InvenTree больше не действует. Войдите заново."
        markup = ui.main_menu(False)
    elif isinstance(exc, InvenTreeError):
        log.warning("ошибка InvenTree: %s", exc)
        text, markup = f"InvenTree ответил ошибкой: {ui.esc(exc.detail[:300])}", None
    else:
        log.exception("необработанная ошибка", exc_info=exc)
        text, markup = "Что-то пошло не так, ошибка записана в лог. Попробуйте ещё раз.", None
        if tg_user:
            store.log_event(tg_user.id, "", "error", error=f"{type(exc).__name__}: {exc}"[:300])
        for admin in cfg.admin_ids:
            try:
                await bot.send_message(admin, f"⚠️ Ошибка бота у {tg_user.id if tg_user else '?'}: "
                                              f"<code>{ui.esc(type(exc).__name__)}: {ui.esc(str(exc)[:300])}</code>")
            except Exception:  # noqa: BLE001 — уведомление не должно ронять обработку ошибки
                log.warning("не удалось уведомить администратора %s", admin)

    if update.callback_query:
        await update.callback_query.answer()
    if origin:
        await origin.answer(text, reply_markup=markup)
    return True
