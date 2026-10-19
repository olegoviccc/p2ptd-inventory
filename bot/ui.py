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
CATALOG = "🗂 Каталог"
DIAG = "🐞 Диагностика"
MENU_TEXTS = {ADD, FIND, PLACE, PROFILE, HELP, JOURNAL, LOGIN, CATALOG, DIAG}


def main_menu(logged_in, is_admin=False):
    if not logged_in:
        rows = [[LOGIN], [HELP]]
    else:
        rows = [[ADD, FIND], [PLACE, PROFILE], [HELP]] + ([[JOURNAL, CATALOG], [DIAG]] if is_admin else [])
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
                               resize_keyboard=True, is_persistent=True)


# --- inline-кнопки


class Loc(CallbackData, prefix="loc"):
    action: str  # venue — показать места заведения, set — выбрать место
    pk: int


class Cat(CallbackData, prefix="cat"):
    action: str  # open — показать подкатегории, set — выбрать
    pk: int      # 0 = корень


class Item(CallbackData, prefix="it"):
    """Действия с позицией склада: open, move, mv_venue, mv_set, status, st_set, batch, note,
    photo, label, delete, del_yes. value — статус или место, если нужно."""
    action: str
    pk: int
    value: int = 0


class Scr(CallbackData, prefix="sc"):
    """Каталог (scraper): home, run, dry, toggle (key), reviews, rev (value=id), acc, rej."""
    action: str
    value: int = 0
    key: str = ""


RECOGNIZE, SAVE, EDIT, CATEGORY, DROP, CHANGE_PLACE, CANCEL, LOGOUT = (
    "recognize", "save", "edit", "category", "drop", "change_place", "cancel", "logout")
LOG_FILE = "logfile:"  # + имя файла лога: прислать его целиком

STATUS_NAMES = {10: "OK", 50: "Требует внимания", 55: "Повреждено", 60: "Уничтожено",
                65: "Отклонено", 70: "Потеряно", 75: "Карантин", 85: "Возвращено"}
# Статусы, которые можно поставить из бота (коды StockStatus InvenTree)
EDITABLE_STATUSES = [10, 50, 55, 70, 60]
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


def item_kb(item):
    pk = item["pk"]
    rows = [
        [("📍 Переместить", Item(action="move", pk=pk)), ("🔧 Статус", Item(action="status", pk=pk))],
        [("🏷 Заводской S/N", Item(action="batch", pk=pk)), ("📝 Заметка", Item(action="note", pk=pk))],
        [("📷 Фото", Item(action="photo", pk=pk)), ("📘 Мануал", Item(action="manual", pk=pk))]
        + ([("🖨 Этикетка", Item(action="label", pk=pk))] if item.get("serial") else []),
        ("🗑 Удалить", Item(action="delete", pk=pk)),
    ]
    return buttons(*rows)


def back_to_item(pk):
    return ("⬅️ К позиции", Item(action="open", pk=pk))


def move_venues_kb(locations, pk):
    venues = sorted((l for l in locations if not l.get("parent")), key=lambda l: l["name"])
    rows = _pairs([(l["name"], Item(action="mv_venue", pk=pk, value=l["pk"])) for l in venues])
    return buttons(*rows, back_to_item(pk))


def move_places_kb(locations, pk, venue_pk):
    places = sorted((l for l in locations if l.get("parent") == venue_pk), key=lambda l: l["name"])
    rows = _pairs([(l["name"], Item(action="mv_set", pk=pk, value=l["pk"])) for l in places])
    return buttons(*rows, ("Само заведение", Item(action="mv_set", pk=pk, value=venue_pk)),
                   ("⬅️ Назад", Item(action="move", pk=pk)))


def status_kb(pk, current):
    rows = [(("• " if s == current else "") + STATUS_NAMES[s], Item(action="st_set", pk=pk, value=s))
            for s in EDITABLE_STATUSES]
    return buttons(*_pairs(rows), back_to_item(pk))


def delete_kb(pk):
    return buttons([("🗑 Да, удалить", Item(action="del_yes", pk=pk)), back_to_item(pk)])


def item_input_kb(pk):
    return buttons(back_to_item(pk))


# --- тексты

HELP_TEXT = (
    f"<b>{ADD}</b> — сфотографируйте шильдик и общий вид, бот сам распознает модель и серийник.\n"
    f"<b>{FIND}</b> — по номеру P2PTD, заводскому серийнику или фото QR-этикетки. "
    "В карточке позиции можно переместить её, сменить статус, исправить S/N, добавить заметку или фото, "
    "перепечатать этикетку.\n"
    f"<b>{PLACE}</b> — где вы сейчас; бот запоминает место для следующих приборов.\n"
    f"<b>{PROFILE}</b> — ваш аккаунт и выход.\n\n"
    "Номер или серийник можно прислать и просто так, без кнопок."
)

ADMIN_HELP = (
    f"\n\n<b>Администраторам:</b> <b>{JOURNAL}</b> — кто что делал и ошибки; <b>{CATALOG}</b> — ночной поиск "
    f"мануалов и фото; <b>{DIAG}</b> — файл с настройками, проверками и логами: если что-то не работает, "
    "перешлите его разработчику."
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
              f"Статус: {STATUS_NAMES.get(item.get('status'), item.get('status'))}"]
    note = (item.get("notes") or "").strip().splitlines()
    if note:
        lines.append(f"Заметка: {esc(note[-1][:200])}")
    lines.append(url)
    return "\n".join(lines)


EVENT_NAMES = {"login": "вошёл", "logout": "вышел", "recognized": "распознал", "saved": "добавил",
               "save_failed": "не смог сохранить", "recognize_failed": "не распознал", "lookup": "искал",
               "moved": "переместил", "status": "сменил статус", "batch": "исправил S/N", "note": "добавил заметку",
               "photo": "добавил фото", "deleted": "удалил", "error": "ошибка",
               "manual_added": "нашёл мануал", "image_added": "нашёл фото модели"}


def journal_text(events):
    if not events:
        return "Журнал пуст."
    lines = []
    for e in events:
        what = ", ".join(f"{k}: {v}" for k, v in e.details.items() if v not in ("", None))
        lines.append(f"<code>{e.ts[5:16].replace('T', ' ')}</code> {esc(e.username or e.tg_id)} "
                     f"{EVENT_NAMES.get(e.action, e.action)} {esc(what)}")
    return "\n".join(lines)


# --- каталог (ночной поиск мануалов и фото)

SETTING_NAMES = {"nightly": "🌙 Ночью", "manuals": "📘 Мануалы", "photos": "🖼 Фото", "verify": "🤖 Проверка моделью"}
SOURCE_NAMES = {"olegovic": "olegovic-llm", "manufacturer": "сайт производителя", "retailer": "магазин",
                "search": "поиск"}


def catalog_text(stats, settings, last_run, progress, reviews, model_name):
    lines = ["🗂 <b>Каталог: мануалы и фото моделей</b>",
             f"Моделей: {stats.parts} (без кода модели: {stats.no_model})",
             f"Без мануала: <b>{stats.missing_manual}</b>, без фото: <b>{stats.missing_photo}</b>"]
    if reviews:
        lines.append(f"📥 Ждут проверки: <b>{reviews}</b>")
    if progress is not None:
        lines.append(f"\n⏳ Идёт поиск: {progress.checked} из {progress.total} моделей, "
                     f"найдено мануалов {len(progress.manuals)}, фото {len(progress.photos)}")
    elif last_run:
        lines.append(f"\nПоследний прогон ({esc(last_run['trigger'])}): {last_run['finished'][5:16].replace('T', ' ')} UTC — "
                     f"мануалов {last_run.get('manuals', 0)}, фото {last_run.get('photos', 0)}, "
                     f"на проверку {last_run.get('review', 0)}, не нашлось {last_run.get('not_found', 0)}")
    flags = ", ".join(f"{name}: {'вкл' if settings[key] else 'выкл'}" for key, name in SETTING_NAMES.items())
    lines.append(f"\n{flags}; за прогон до {settings['limit']} моделей")
    lines.append(f"Модель для проверки: {esc(model_name) if model_name else 'не настроена'}")
    return "\n".join(lines)


def catalog_kb(settings, running, reviews, can_verify):
    rows = []
    if not running:
        rows.append([(f"▶️ Запустить ({settings['limit']})", Scr(action="run")),
                     ("🧪 Без записи (3)", Scr(action="dry"))])
    if reviews:
        rows.append((f"📥 На проверку ({reviews})", Scr(action="reviews")))
    toggles = [(f"{'✅' if settings[k] else '⬜'} {name}", Scr(action="toggle", key=k))
               for k, name in SETTING_NAMES.items() if k != "verify" or can_verify]
    rows += _pairs(toggles)
    rows.append([("➖ 5", Scr(action="limit", value=-5)), ("➕ 5", Scr(action="limit", value=5)),
                 ("🔄 Обновить", Scr(action="home"))])
    return buttons(*rows)


def review_caption(r):
    what = "мануал" if r.kind == "manual" else "фото"
    return (f"📥 <b>{esc(r.part_name)}</b> — {what}?\n"
            f"Источник: {SOURCE_NAMES.get(r.source, r.source)}{' — ' + esc(r.url) if r.url else ''}\n"
            f"Модель: {esc(r.reason) or 'без пояснения'}")


def review_kb(review_id):
    return buttons([("✅ Принять", Scr(action="acc", value=review_id)), ("❌ Отклонить", Scr(action="rej", value=review_id))],
                   ("⬅️ К каталогу", Scr(action="home")))
