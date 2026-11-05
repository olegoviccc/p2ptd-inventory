"""Отправка этикеток одному получателю (BOT_LABEL_RECIPIENT, например тому, у кого принтер).

Под каждой новой этикеткой у остальных пользователей кнопки «📤 Отправить» и «🔁 Отправлять всегда».
Если автоотправка включена, этикетка уходит получателю сразу, а под ней кнопка «⏹ Не отправлять».
"""

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

from .. import ui

log = logging.getLogger(__name__)
router = Router(name="share")


def recipient_id(cfg, store, user):
    """Telegram ID получателя или None: не настроен, ещё не входил в бота или это он сам."""
    if not cfg.label_recipient or not user or user.username.lower() == cfg.label_recipient.lower():
        return None
    return store.tg_id_of(cfg.label_recipient)


def share_buttons(cfg, store, user):
    """Ряды кнопок под этикеткой (пусто, если отправлять некому)."""
    if recipient_id(cfg, store, user) is None:
        return ()
    if user.share_labels:
        return (("⏹ Не отправлять этикетки автоматически", ui.SHARE_OFF),)
    return ([(f"📤 Отправить {cfg.label_recipient}", ui.SHARE_SEND), ("🔁 Отправлять всегда", ui.SHARE_ALWAYS)],)


async def send(bot: Bot, cfg, store, user, message: Message):
    """Копия сообщения с этикеткой получателю. True, если ушло."""
    to = recipient_id(cfg, store, user)
    if to is None or not message.document:
        return False
    caption = f"🏷 от {user.username}\n{message.caption or ''}".strip()
    try:
        await bot.send_document(to, message.document.file_id, caption=caption[:1024])
    except TelegramAPIError as e:
        log.warning("этикетка не ушла %s: %s", cfg.label_recipient, e)
        return False
    store.log_event(user.tg_id, user.username, "label_shared", to=cfg.label_recipient, label=message.document.file_name)
    return True


async def after_label(bot: Bot, cfg, store, user, message: Message):
    """Вызывать после отправки этикетки пользователю: при включённой автоотправке — сразу получателю."""
    if user and user.share_labels and await send(bot, cfg, store, user, message):
        await message.reply(f"📤 Отправлено {cfg.label_recipient}")


@router.callback_query(F.data.in_({ui.SHARE_SEND, ui.SHARE_ALWAYS}))
async def share(call: CallbackQuery, bot: Bot, cfg, store, user):
    if call.data == ui.SHARE_ALWAYS:
        store.set_share_labels(user.tg_id, True)
        user.share_labels = True
    if not await send(bot, cfg, store, user, call.message):
        return await call.answer(f"Не получилось отправить: {cfg.label_recipient} должен хоть раз войти в бота.",
                                 show_alert=True)
    note = " Новые этикетки будут уходить автоматически." if call.data == ui.SHARE_ALWAYS else ""
    await call.answer(f"Отправлено {cfg.label_recipient}.{note}", show_alert=bool(note))
    await call.message.edit_reply_markup(reply_markup=ui.buttons(*keep_other_rows(call.message),
                                                                 *share_buttons(cfg, store, user)) or None)


@router.callback_query(F.data == ui.SHARE_OFF)
async def share_off(call: CallbackQuery, cfg, store, user):
    store.set_share_labels(user.tg_id, False)
    user.share_labels = False
    await call.answer("Автоотправка выключена.")
    await call.message.edit_reply_markup(reply_markup=ui.buttons(*keep_other_rows(call.message),
                                                                 *share_buttons(cfg, store, user)))


def keep_other_rows(message: Message):
    """Остальные кнопки под этикеткой («Изменить», печать) без кнопок отправки."""
    rows = []
    for row in (message.reply_markup.inline_keyboard if message.reply_markup else []):
        kept = [(b.text, b.url or b.callback_data) for b in row if not (b.callback_data or "").startswith("share:")]
        if kept:
            rows.append(kept[0] if len(kept) == 1 else kept)
    return rows
