"""Доступ к боту: новый человек пишет → администраторам приходит запрос с кнопками «Пустить» / «Отклонить».

Белый список из .env (BOT_ALLOWED_TELEGRAM_IDS, BOT_ADMIN_IDS) по-прежнему работает; кого пустили кнопкой,
хранится в bot.sqlite3 (таблица access). Если BOT_ALLOWED_TELEGRAM_IDS пуст, бот открыт всем и запросов нет.
"""

import html
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery

from .. import ui

log = logging.getLogger(__name__)
router = Router(name="access")


def allowed(cfg, store, tg_id):
    return not cfg.allowed_ids or tg_id in cfg.allowed_ids or store.access(tg_id) == "approved"


def describe(tg_user):
    name = tg_user.full_name or str(tg_user.id)
    return f"{name} (@{tg_user.username})" if tg_user.username else name


async def request(bot: Bot, cfg, store, tg_user):
    """Первое сообщение от незнакомца: запомнить запрос и спросить администраторов. Вернуть ответ человеку."""
    status = store.access(tg_user.id)
    if status == "denied":
        log.info("отклонён: доступ закрыт администратором")
        return None
    if status == "pending":
        log.info("отклонён: запрос ждёт администратора")
        return "⏳ Запрос на доступ ещё у администратора. Напишу, когда он ответит."

    name = describe(tg_user)
    store.set_access(tg_user.id, "pending", name)
    log.info("запрос доступа от %s", name)
    text = f"🙋 Просит доступ к боту: <b>{html.escape(name)}</b>\nTelegram ID: <code>{tg_user.id}</code>"
    sent = 0
    for admin in cfg.admin_ids:
        try:
            await bot.send_message(admin, text, reply_markup=ui.access_request_kb(tg_user.id))
            sent += 1
        except TelegramAPIError as e:
            log.warning("запрос доступа не ушёл администратору %s: %s", admin, e)
    if not sent:
        log.warning("запрос доступа от %s некому показать: администраторы не писали боту или BOT_ADMIN_IDS пуст", name)
    return "🔒 Доступа к боту пока нет. Отправил запрос администратору — напишу, когда он ответит."


@router.callback_query(F.data.startswith(ui.ACCESS_OK) | F.data.startswith(ui.ACCESS_NO))
async def decide(call: CallbackQuery, bot: Bot, cfg, store, is_admin, user):
    if not is_admin:
        return await call.answer("Это может только администратор.", show_alert=True)
    ok = call.data.startswith(ui.ACCESS_OK)
    tg_id = int(call.data.rsplit(":", 1)[1])
    name = store.access_name(tg_id)
    store.set_access(tg_id, "approved" if ok else "denied")
    if not ok:
        store.delete(tg_id)  # если уже входил — выйти из InvenTree
    store.log_event(call.from_user.id, user.username if user else "", "access_granted" if ok else "access_denied",
                    кто=name, id=tg_id)
    log.info("%s: %s (%s)", "пущен" if ok else "отклонён", name, tg_id)

    who = f"<b>{html.escape(name)}</b> (<code>{tg_id}</code>)"
    if ok:
        text, kb = f"✅ Пущен: {who}", ui.buttons(("🚫 Закрыть доступ", f"{ui.ACCESS_NO}{tg_id}"))
    else:
        text, kb = f"⛔ Доступ закрыт: {who}", ui.buttons(("✅ Всё-таки пустить", f"{ui.ACCESS_OK}{tg_id}"))
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()
    if ok:
        try:
            await bot.send_message(tg_id, "✅ Администратор открыл доступ. Нажмите /start и войдите своим "
                                          "аккаунтом InvenTree.")
        except TelegramAPIError as e:
            log.warning("не смог сообщить %s о доступе: %s", tg_id, e)
