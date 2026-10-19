"""«🗂 Каталог» для администраторов: управление ночным поиском мануалов и фото (scraper)."""

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from .. import ui

log = logging.getLogger(__name__)
router = Router(name="catalog")

NOT_CONFIGURED = ("Каталог не настроен: нужен INVENTREE_TOKEN в .env и зависимости "
                  "<code>.venv/bin/pip install -r scraper/requirements.txt</code>, затем перезапуск бота.")
_background = set()  # ссылки на фоновые прогоны, чтобы их не собрал сборщик мусора


def in_background(coro):
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def screen(scraper):
    settings = scraper.settings()
    text = ui.catalog_text(await scraper.stats(), settings, scraper.state.last_run(), scraper.progress,
                           len(scraper.state.reviews()), scraper.llm.model if scraper.llm else "")
    return text, ui.catalog_kb(settings, scraper.running, len(scraper.state.reviews()), scraper.llm is not None)


async def guard(event, is_admin, scraper):
    """Текст отказа или None, если можно."""
    if not is_admin:
        return "Каталог доступен только администраторам."
    if scraper is None:
        return NOT_CONFIGURED
    return None


@router.message(Command("catalog"))
@router.message(F.text == ui.CATALOG)
async def open_catalog(message: Message, is_admin, scraper=None):
    if problem := await guard(message, is_admin, scraper):
        return await message.answer(problem)
    text, kb = await screen(scraper)
    await message.answer(text, reply_markup=kb)


@router.callback_query(ui.Scr.filter())
async def catalog_action(call: CallbackQuery, callback_data: ui.Scr, bot: Bot, user, is_admin, scraper=None):
    if problem := await guard(call, is_admin, scraper):
        return await call.answer(problem.split(":")[0], show_alert=True)
    action = callback_data.action

    if action in ("run", "dry"):
        dry = action == "dry"
        if scraper.running:
            return await call.answer("Поиск уже идёт", show_alert=True)
        in_background(run_and_report(bot, call.message.chat.id, scraper, user.username, dry))
        await call.answer("Проверяю без записи…" if dry else "Запустил поиск, пришлю отчёт")
        await asyncio.sleep(0)  # дать прогону стартовать, чтобы экран показал прогресс
    elif action == "toggle":
        settings = scraper.settings()
        scraper.state.set_setting(callback_data.key, not settings[callback_data.key])
        await call.answer()
    elif action == "limit":
        limit = max(1, min(200, scraper.settings()["limit"] + callback_data.value))
        scraper.state.set_setting("limit", limit)
        await call.answer(f"За прогон: {limit}")
    elif action == "reviews":
        await call.answer()
        return await show_review(call.message, scraper)
    elif action in ("acc", "rej"):
        return await decide(call, scraper, callback_data.value, accepted=action == "acc", author=user.username)
    else:
        await call.answer()

    text, kb = await screen(scraper)
    if call.message.text:
        await call.message.edit_text(text, reply_markup=kb)
    else:
        await call.message.answer(text, reply_markup=kb)


async def run_and_report(bot, chat_id, scraper, author, dry_run):
    from scraper.engine import Busy
    from scraper.run import format_report
    try:
        report = await scraper.run(f"бот: {author}", dry_run=dry_run, limit=3 if dry_run else None)
    except Busy as e:
        return await bot.send_message(chat_id, f"Не запущено: {e}.")
    except Exception as e:  # noqa: BLE001 — фоновая задача: сообщаем, а не роняем бота
        log.exception("прогон из бота упал")
        return await bot.send_message(chat_id, f"⚠️ Поиск прервался: {ui.esc(e)}")
    markup = ui.buttons(("🗂 К каталогу", ui.Scr(action="home")))
    await bot.send_message(chat_id, format_report(report, dry_run), reply_markup=markup)


async def show_review(message, scraper):
    reviews = scraper.state.reviews()
    if not reviews:
        return await message.answer("📥 Очередь пуста.", reply_markup=ui.buttons(("⬅️ К каталогу", ui.Scr(action="home"))))
    r = reviews[0]
    caption = ui.review_caption(r) + (f"\n\nЕщё в очереди: {len(reviews) - 1}" if len(reviews) > 1 else "")
    file = BufferedInputFile(r.path.read_bytes(), r.path.name.split("_", 1)[-1])
    if r.kind == "photo":
        await message.answer_photo(file, caption=caption, reply_markup=ui.review_kb(r.id))
    else:
        await message.answer_document(file, caption=caption, reply_markup=ui.review_kb(r.id))


async def decide(call, scraper, review_id, accepted, author):
    r = await scraper.accept(review_id, author) if accepted else scraper.reject(review_id)
    if not r:
        await call.answer("Уже решено", show_alert=True)
    else:
        verdict = "✅ Принято, приложено в InvenTree" if accepted else "❌ Отклонено, больше не предложу"
        await call.answer(verdict.split(",")[0])
        await call.message.edit_caption(caption=f"{ui.review_caption(r)}\n\n<b>{verdict}</b>", reply_markup=None)
    await show_review(call.message, scraper)
