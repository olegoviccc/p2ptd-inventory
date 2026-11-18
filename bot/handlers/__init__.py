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
    saving = State()  # сохранение идёт: повторное нажатие «Сохранить» ничего не делает
    edit = State()


class Find(StatesGroup):
    query = State()


class ItemEdit(StatesGroup):
    batch = State()
    note = State()
    photo = State()


_routers = None


def routers():
    global _routers
    if _routers is None:
        from . import access, add, auth, catalog, common, item, lookup, share

        for r in (add.router, item.router, catalog.router, lookup.router, share.router):
            r.message.middleware(RequireLogin())
            r.callback_query.middleware(RequireLogin())
        _routers = [access.router, common.router, auth.router, add.router, item.router, catalog.router, lookup.router,
                    share.router, common.fallback]
    return _routers
