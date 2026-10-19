"""
Фоновый поиск мануалов и фото для моделей в InvenTree.

  python -m scraper --stats        сколько моделей без фото и без мануала
  python -m scraper --dry-run      найти, но ничего не записывать в InvenTree
  python -m scraper                найти и приложить (по умолчанию до 20 моделей за запуск)
  python -m scraper --part 42      только одна деталь (не ждать 30 дней после неудачи)
  python -m scraper --only photos  только фото (или --only manuals)
  python -m scraper --nightly      ночной запуск по таймеру: пропускается, если ночной поиск выключен в боте

Управлять удобнее из бота: «🗂 Каталог» (для администраторов).
Настройки — SCRAPER_* в .env (см. .env.example) и переключатели в боте.
"""

import argparse
import asyncio
import logging
from pathlib import Path

from bot import logs
from bot.inventree import Api
from bot.storage import Storage

from . import config
from .engine import Busy, build
from .run import format_report

log = logging.getLogger("scraper")


async def notify(cfg, web, text):
    """Отчёт администраторам тем же Telegram-ботом."""
    if not (cfg.telegram_token and cfg.admin_ids):
        return
    for admin in cfg.admin_ids:
        try:
            async with web.session().post(f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage",
                                          json={"chat_id": admin, "text": text, "parse_mode": "HTML"}) as resp:
                if resp.status >= 400:
                    log.warning("отчёт в Telegram для %s: %s", admin, resp.status)
        except Exception as e:  # noqa: BLE001 — без отчёта прогон всё равно состоялся
            log.warning("отчёт в Telegram для %s не отправлен: %s", admin, e)


async def main(args):
    cfg = config.load()
    logs.setup(cfg.data_dir, cfg.log_level, filename="scraper.log")
    api = Api(cfg.inventree_url, timeout=120)
    scraper = build(cfg, api, Storage(Path(cfg.data_dir) / "bot.sqlite3"))
    try:
        if args.stats:
            print(format_report(await scraper.stats(), dry_run=True))
            return
        settings = scraper.settings()
        if args.nightly and not settings["nightly"]:
            log.info("ночной поиск выключен в боте («🗂 Каталог»), пропускаю")
            return
        log.info("старт: поиск %s, настройки %s, папка olegovic: %s", scraper.search.name, settings,
                 cfg.olegovic_manuals)
        try:
            report = await scraper.run("ночь" if args.nightly else "вручную", dry_run=args.dry_run,
                                       limit=args.limit, only=args.only, part_pk=args.part, force=bool(args.part))
        except Busy as e:
            log.info("не запускаю: %s", e)
            return
        text = format_report(report, args.dry_run)
        print(text.replace("<b>", "").replace("</b>", ""))
        if not args.dry_run and (report.manuals or report.photos or report.review or report.errors):
            await notify(cfg, scraper.web, text)
    finally:
        await scraper.close()
        await api.close()


def cli():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--stats", action="store_true", help="только посчитать, чего не хватает")
    ap.add_argument("--dry-run", action="store_true", help="искать, но не записывать в InvenTree")
    ap.add_argument("--limit", type=int, help="сколько моделей обработать за запуск")
    ap.add_argument("--only", choices=["manuals", "photos"], help="только мануалы или только фото")
    ap.add_argument("--part", type=int, help="pk одной детали в InvenTree")
    ap.add_argument("--nightly", action="store_true", help="запуск по таймеру (учитывает выключатель в боте)")
    asyncio.run(main(ap.parse_args()))


if __name__ == "__main__":
    cli()
