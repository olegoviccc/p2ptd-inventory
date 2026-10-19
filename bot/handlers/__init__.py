"""Роутеры бота. Порядок важен: кнопки меню и общие команды раньше, чем ввод текста в сценариях."""

from aiogram.fsm.state import State, StatesGroup

from ..middlewares import RequireLogin


class Login(StatesGroup):
    username = State()
    password = State()


class Add(StatesGroup):
    venue = State()
    photos = State()
    confirm = State()
    edit = State()


class Find(StatesGroup):
    query = State()


def routers():
    from . import add, auth, common, lookup

    for r in (add.router, lookup.router):
        r.message.middleware(RequireLogin())
        r.callback_query.middleware(RequireLogin())
    return [common.router, auth.router, add.router, lookup.router]
