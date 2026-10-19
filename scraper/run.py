"""Прогон: какие модели без фото и мануала → поиск → вложения и картинки в InvenTree → отчёт."""

import asyncio
import logging
from dataclasses import dataclass, field

from .finders import find_manual, find_photo

log = logging.getLogger(__name__)

MANUAL_WORDS = ("руководство", "мануал", "manual", "инструкция")


@dataclass
class Target:
    pk: int
    name: str
    manufacturer: str
    model: str
    needs_manual: bool
    needs_photo: bool


@dataclass
class Report:
    parts: int = 0
    no_model: int = 0          # детали без производителя и MPN (LED Par и т. п.) — искать нечего
    missing_manual: int = 0
    missing_photo: int = 0
    checked: int = 0
    manuals: list = field(default_factory=list)      # (модель, источник)
    photos: list = field(default_factory=list)
    not_found: list = field(default_factory=list)    # (модель, что не нашлось)
    errors: list = field(default_factory=list)


def is_manual(attachment):
    """Вложение похоже на мануал: PDF-файл или ссылка, либо подпись со словом «руководство»."""
    name = (attachment.get("filename") or attachment.get("attachment") or attachment.get("link") or "").lower()
    comment = (attachment.get("comment") or "").lower()
    return name.split("?")[0].endswith(".pdf") or any(w in comment for w in MANUAL_WORDS)


async def targets(it, report):
    parts, mpns, companies, attachments = await asyncio.gather(
        it.get_all("/api/part/", active="true"),
        it.get_all("/api/company/part/manufacturer/"),
        it.get_all("/api/company/"),
        it.get_all("/api/attachment/", model_type="part"),
    )
    company = {c["pk"]: c["name"] for c in companies}
    mpn_of = {}
    for m in mpns:
        mpn_of.setdefault(m["part"], m)
    with_manual = {int(a["model_id"]) for a in attachments if is_manual(a)}

    report.parts = len(parts)
    result = []
    for p in parts:
        m = mpn_of.get(p["pk"])
        if not m or not m.get("MPN") or m.get("manufacturer") not in company:
            report.no_model += 1
            continue
        t = Target(p["pk"], p["name"], company[m["manufacturer"]], m["MPN"],
                   needs_manual=p["pk"] not in with_manual, needs_photo=not p.get("image"))
        report.missing_manual += t.needs_manual
        report.missing_photo += t.needs_photo
        result.append(t)
    return result


async def run(cfg, it, search, web, sources, state, store=None, *, only=None, limit=None,
              dry_run=False, part_pk=None, force=False):
    report = Report()
    do_manuals = cfg.manuals and only in (None, "manuals")
    do_photos = cfg.photos and only in (None, "photos")
    limit = limit or cfg.limit

    for t in await targets(it, report):
        if part_pk and t.pk != part_pk:
            continue
        manual = do_manuals and t.needs_manual and (force or state.due(t.pk, "manual", cfg.retry_days))
        photo = do_photos and t.needs_photo and (force or state.due(t.pk, "photo", cfg.retry_days))
        if not (manual or photo):
            continue
        if report.checked >= limit:
            break
        report.checked += 1
        log.info("ищу для %s:%s%s", t.name, " мануал" if manual else "", " фото" if photo else "")
        try:
            if manual:
                await _manual(cfg, it, search, web, sources, state, store, t, report, dry_run)
            if photo:
                await _photo(it, search, web, sources, state, store, t, report, dry_run)
        except Exception as e:  # noqa: BLE001 — одна модель не должна останавливать весь прогон
            log.exception("ошибка на %s", t.name)
            report.errors.append(f"{t.name}: {type(e).__name__}: {e}"[:200])
    return report


async def _manual(cfg, it, search, web, sources, state, store, t, report, dry_run):
    found = await find_manual({"manufacturer": t.manufacturer, "model": t.model}, search, web, sources,
                              cfg.olegovic_manuals, cfg.manual_search)
    if not found:
        report.not_found.append((t.name, "мануал"))
        if not dry_run:
            state.record(t.pk, "manual", "not_found")
        return
    log.info("мануал для %s: %s (%s)", t.name, found.url or "папка olegovic", found.source)
    report.manuals.append((t.name, found.source))
    if dry_run:
        return
    comment = f"Руководство пользователя (источник: {found.url or 'olegovic-llm'})"[:250]
    if cfg.manual_mode == "link" and found.url:
        await it.attach_link("part", t.pk, found.url, comment)
    else:
        await it.attach("part", t.pk, found.filename, found.data, comment)
    state.record(t.pk, "manual", "found", found.source, found.url)
    if store:
        store.log_event(0, "scraper", "manual_added", model=t.name, source=found.source)


async def _photo(it, search, web, sources, state, store, t, report, dry_run):
    found = await find_photo({"manufacturer": t.manufacturer, "model": t.model}, search, web, sources)
    if not found:
        report.not_found.append((t.name, "фото"))
        if not dry_run:
            state.record(t.pk, "photo", "not_found")
        return
    log.info("фото для %s: %s (%s)", t.name, found.url, found.source)
    report.photos.append((t.name, found.source))
    if dry_run:
        return
    await it.set_part_image(t.pk, found.filename, found.data)
    state.record(t.pk, "photo", "found", found.source, found.url)
    if store:
        store.log_event(0, "scraper", "image_added", model=t.name, source=found.source)


SOURCE_NAMES = {"olegovic": "olegovic-llm", "manufacturer": "сайт производителя", "retailer": "магазин",
                "search": "поиск"}


def format_report(r, dry_run=False):
    head = "🔎 Каталог (проверка, без записи)" if dry_run else "🔎 Каталог: ночной поиск"
    lines = [f"<b>{head}</b>",
             f"Моделей: {r.parts}, без модели производителя: {r.no_model}",
             f"Без мануала: {r.missing_manual}, без фото: {r.missing_photo}",
             f"Проверено за прогон: {r.checked}"]
    if r.manuals:
        lines.append("\n📘 Мануалы: " + ", ".join(f"{n} ({SOURCE_NAMES.get(s, s)})" for n, s in r.manuals))
    if r.photos:
        lines.append("🖼 Фото: " + ", ".join(f"{n} ({SOURCE_NAMES.get(s, s)})" for n, s in r.photos))
    if r.not_found:
        lines.append("Не нашлось: " + ", ".join(f"{n} — {what}" for n, what in r.not_found[:15])
                     + (f" и ещё {len(r.not_found) - 15}" if len(r.not_found) > 15 else ""))
    if r.errors:
        lines.append("⚠️ Ошибки: " + "; ".join(r.errors[:5]))
    return "\n".join(lines)
