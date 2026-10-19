"""
Фоновый поиск мануалов и фото для моделей в InvenTree.

  python -m scraper --stats        сколько моделей без фото и без мануала
  python -m scraper --dry-run      найти, но ничего не записывать в InvenTree
  python -m scraper                найти и приложить (по умолчанию до 20 моделей за запуск)
  python -m scraper --part 42      только одна деталь (не ждать 30 дней после неудачи)
  python -m scraper --only photos  только фото (или --only manuals)

Обычно запускается ночью таймером systemd (systemd/p2ptd-scraper.timer).
Настройки — SCRAPER_* в .env (см. .env.example).
"""

import argparse
import asyncio
import logging
from pathlib import Path

from bot import logs
from bot.inventree import Api
from bot.storage import Storage

from . import config
from .run import format_report, run, targets, Report
from .search import BraveSearch, DDGSearch
from .sources import Sources
from .state import State
from .web import Web

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
    it = api.client(cfg.inventree_token)
    web = Web()
    try:
        if args.stats:
            report = Report()
            await targets(it, report)
            print(format_report(report, dry_run=True))
            return
        search = BraveSearch(cfg.brave_key, web.session) if cfg.search == "brave" else DDGSearch()
        log.info("старт: поиск %s, мануалы %s (папка olegovic: %s), фото %s, до %d моделей",
                 search.name, cfg.manuals, cfg.olegovic_manuals, cfg.photos, args.limit or cfg.limit)
        report = await run(cfg, it, search, web, Sources.load(), State(Path(cfg.data_dir) / "scraper.sqlite3"),
                           None if args.dry_run else Storage(Path(cfg.data_dir) / "bot.sqlite3"),
                           only=args.only, limit=args.limit, dry_run=args.dry_run,
                           part_pk=args.part, force=bool(args.part))
        text = format_report(report, args.dry_run)
        print(text.replace("<b>", "").replace("</b>", ""))
        if not args.dry_run and (report.manuals or report.photos or report.errors):
            await notify(cfg, web, text)
    finally:
        await web.close()
        await api.close()


def cli():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--stats", action="store_true", help="только посчитать, чего не хватает")
    ap.add_argument("--dry-run", action="store_true", help="искать, но не записывать в InvenTree")
    ap.add_argument("--limit", type=int, help="сколько моделей обработать за запуск")
    ap.add_argument("--only", choices=["manuals", "photos"], help="только мануалы или только фото")
    ap.add_argument("--part", type=int, help="pk одной детали в InvenTree")
    asyncio.run(main(ap.parse_args()))


if __name__ == "__main__":
    cli()
