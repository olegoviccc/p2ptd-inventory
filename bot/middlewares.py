"""Middleware: доступ, пользователь и клиент InvenTree для обработчиков, лог событий."""

import logging
import time

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message

from . import logs, ui

log = logging.getLogger("bot.events")

SECRET_STATES = {"Login:password"}


def describe(event, raw_state):
    if isinstance(event, CallbackQuery):
        return f"кнопка {event.data}"
    if raw_state in SECRET_STATES:
        return "текст ***"
    if event.photo:
        return "фото"
    if event.document:
        return f"файл {event.document.mime_type}"
    return f"текст {event.text!r}" if event.text else event.content_type


class Context(BaseMiddleware):
    """Внешний: проверяет доступ (новичков отправляет на одобрение администратору), кладёт user и it (клиент InvenTree) в обработчик, пишет лог."""

    async def __call__(self, handler, event, data):
        tg_user = data.get("event_from_user")
        cfg = data["cfg"]
        if not tg_user:
            return await handler(event, data)
        token = logs.current_user.set(str(tg_user.id))
        try:
            from .handlers import access  # здесь, а не наверху: handlers импортирует этот модуль
            if not access.allowed(cfg, data["store"], tg_user.id):
                reply = await access.request(data["bot"], cfg, data["store"], tg_user)
                if reply and isinstance(event, Message):
                    await event.answer(reply)
                elif reply and isinstance(event, CallbackQuery):
                    await event.answer(reply, show_alert=True)
                return None
            user = data["store"].get(tg_user.id)
            data["user"] = user
            data["it"] = data["api"].client(user.token) if user else None
            data["is_admin"] = tg_user.id in cfg.admin_ids

            started = time.monotonic()
            log.info("→ %s (state %s)", describe(event, data.get("raw_state")), data.get("raw_state"))
            try:
                return await handler(event, data)
            finally:
                log.info("← за %.0f мс", (time.monotonic() - started) * 1000)
        finally:
            logs.current_user.reset(token)


class RequireLogin(BaseMiddleware):
    """Внутренний (на роутерах add и lookup): без входа отвечает подсказкой."""

    async def __call__(self, handler, event, data):
        if data.get("it"):
            return await handler(event, data)
        text = "Сначала войдите своим аккаунтом InvenTree."
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        elif isinstance(event, Message):
            await event.answer(text, reply_markup=ui.main_menu(False))
        return None
