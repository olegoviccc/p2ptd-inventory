"""Кнопки и тексты бота."""

import html

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

# --- главное меню (кнопки внизу экрана)

ADD = "➕ Добавить прибор"
FIND = "🔎 Найти"
PLACE = "📍 Место"
PROFILE = "👤 Профиль"
HELP = "❓ Помощь"
JOURNAL = "📜 Журнал"
LOGIN = "🔑 Войти"
MENU_TEXTS = {ADD, FIND, PLACE, PROFILE, HELP, JOURNAL, LOGIN}


def main_menu(logged_in, is_admin=False):
    if not logged_in:
        rows = [[LOGIN], [HELP]]
    else:
        rows = [[ADD, FIND], [PLACE, PROFILE], [HELP, JOURNAL] if is_admin else [HELP]]
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
                               resize_keyboard=True, is_persistent=True)


# --- inline-кнопки


class Loc(CallbackData, prefix="loc"):
    action: str  # venue — показать места заведения, set — выбрать место
    pk: int


class Cat(CallbackData, prefix="cat"):
    action: str  # open — показать подкатегории, set — выбрать
    pk: int      # 0 = корень


RECOGNIZE, SAVE, EDIT, CATEGORY, DROP, CHANGE_PLACE, CANCEL, LOGOUT = (
    "recognize", "save", "edit", "category", "drop", "change_place", "cancel", "logout")

STATUS_NAMES = {10: "OK", 50: "Требует внимания", 55: "Повреждено", 60: "Уничтожено",
                65: "Отклонено", 70: "Потеряно", 75: "Карантин", 85: "Возвращено"}
CONFIDENCE = {"high": "высокая", "medium": "средняя", "low": "низкая"}


def esc(text):
    return html.escape(str(text or ""))


def _button(text, data):
    return InlineKeyboardButton(text=text, callback_data=data.pack() if isinstance(data, CallbackData) else data)


def buttons(*rows):
    """buttons(("Текст", data), [("A", a), ("B", b)]): ряд — кортеж (одна кнопка) или список кортежей."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [_button(t, d) for t, d in ([row] if isinstance(row, tuple) else row)] for row in rows])


def _pairs(items):
    return [items[i:i + 2] for i in range(0, len(items), 2)]


def cancel_kb():
    return buttons(("✖️ Отмена", CANCEL))


def photos_kb(count):
    return buttons((f"🔍 Распознать ({count} фото)", RECOGNIZE), ("📍 Сменить место", CHANGE_PLACE))


def venues_kb(locations, last_pk=None, last_label=""):
    rows = [(f"📍 Как в прошлый раз: {last_label}", Loc(action="set", pk=last_pk))] if last_pk else []
    venues = sorted((l for l in locations if not l.get("parent")), key=lambda l: l["name"])
    rows += _pairs([(l["name"], Loc(action="venue", pk=l["pk"])) for l in venues])
    rows.append(("✖️ Отмена", CANCEL))
    return buttons(*rows)


def places_kb(locations, venue_pk):
    places = sorted((l for l in locations if l.get("parent") == venue_pk), key=lambda l: l["name"])
    rows = _pairs([(l["name"], Loc(action="set", pk=l["pk"])) for l in places])
    rows += [("Само заведение", Loc(action="set", pk=venue_pk)), ("⬅️ Назад", CHANGE_PLACE)]
    return buttons(*rows)


def categories_kb(categories, paths, parent):
    rows = [(f"✅ {paths[parent]}", Cat(action="set", pk=parent))] if parent else []
    has_children = {c.get("parent") for c in categories}
    for c in sorted((c for c in categories if (c.get("parent") or 0) == parent), key=lambda c: c["name"]):
        opens = c["pk"] in has_children
        rows.append((c["name"] + (" ›" if opens else ""), Cat(action="open" if opens else "set", pk=c["pk"])))
    rows.append(("⬅️ К карточке", Cat(action="back", pk=0)))
    return buttons(*rows)


def card_kb(draft):
    rows = []
    if draft.can_save:
        rows.append(("✅ Сохранить", SAVE))
    rows.append([("✏️ Исправить", EDIT), ("🗂 Категория", CATEGORY)] if not draft.part_pk else ("✏️ Исправить", EDIT))
    rows.append(("✖️ Отмена", DROP))
    return buttons(*rows)


# --- тексты

HELP_TEXT = (
    f"<b>{ADD}</b> — сфотографируйте шильдик и общий вид, бот сам распознает модель и серийник.\n"
    f"<b>{FIND}</b> — по номеру P2PTD, заводскому серийнику или фото QR-этикетки.\n"
    f"<b>{PLACE}</b> — где вы сейчас; бот запоминает место для следующих приборов.\n"
    f"<b>{PROFILE}</b> — ваш аккаунт и выход.\n\n"
    "Номер или серийник можно прислать и просто так, без кнопок."
)


def card_text(d):
    lines = [f"<b>{esc(d.name) or 'Модель не распознана'}</b>"]
    if d.serial:
        mark = "✓ совпал со штрихкодом" if d.serial_confirmed else "⚠️ сверьте с фото"
        lines.append(f"S/N: <code>{esc(d.serial)}</code> {mark}")
    else:
        lines.append("S/N: не найден (если он есть, нажмите «Исправить»)")
    lines += [f"Категория: {esc(d.category_label) or 'не выбрана'}",
              f"Место: {esc(d.location_label)}",
              "В каталоге: есть" if d.part_pk else "В каталоге: нет, модель будет создана"]
    if d.rider_stock_pk:
        lines.append(f"Из райдера: здесь {d.rider_qty:g} шт. не проверено, одна будет сверена")
    if d.duplicate:
        lines.append(f"\n⛔ Прибор с этим S/N уже заведён: <b>{esc(d.duplicate)}</b>")
    if d.confidence:
        lines.append(f"\nУверенность: {CONFIDENCE.get(d.confidence, d.confidence)}")
    if d.notes:
        lines.append(f"Заметка: {esc(d.notes)}")
    return "\n".join(lines)


def item_text(item, url):
    part = item.get("part_detail") or {}
    lines = [f"<b>{esc(item.get('serial') or '#' + str(item['pk']))}</b> — {esc(part.get('full_name') or part.get('name'))}"]
    if item.get("batch"):
        lines.append(f"Заводской S/N: <code>{esc(item['batch'])}</code>")
    if not item.get("serial"):
        lines.append(f"Количество: {float(item.get('quantity', 0)):g}")
    lines += [f"Место: {esc((item.get('location_detail') or {}).get('pathstring', '—'))}",
              f"Статус: {STATUS_NAMES.get(item.get('status'), item.get('status'))}",
              url]
    return "\n".join(lines)


EVENT_NAMES = {"login": "вошёл", "logout": "вышел", "recognized": "распознал", "saved": "добавил",
               "save_failed": "не смог сохранить", "recognize_failed": "не распознал", "lookup": "искал",
               "error": "ошибка"}


def journal_text(events):
    if not events:
        return "Журнал пуст."
    lines = []
    for e in events:
        what = ", ".join(f"{k}: {v}" for k, v in e.details.items() if v not in ("", None))
        lines.append(f"<code>{e.ts[5:16].replace('T', ' ')}</code> {esc(e.username or e.tg_id)} "
                     f"{EVENT_NAMES.get(e.action, e.action)} {esc(what)}")
    return "\n".join(lines)
