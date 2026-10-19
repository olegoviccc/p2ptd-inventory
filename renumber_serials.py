#!/usr/bin/env python3
"""
Переводит внутренние номера в новый формат: P2PTD-00008 → P2PTD0008.

  1. Посмотреть, что изменится:
       python renumber_serials.py --dry-run

  2. Переименовать:
       python renumber_serials.py

QR на уже наклеенных этикетках продолжают работать: в них зашит номер позиции
в InvenTree, а не текст номера. Перепечатать этикетку можно из бота
(карточка позиции → «🖨 Этикетка»).

Адрес и токен берутся из .env. Нужны права Stock Items: View и Change.
Скрипт можно запускать повторно: номера в новом формате не трогаются.
"""

from bot.numbering import Numbering
from inventree_client import base_parser, connect

STOCK_API = "/api/stock/"


def plan(items, numbering):
    """[(pk, старый, новый)] для номеров не в текущем формате; занятые новые номера пропускаются."""
    taken = {i["serial"] for i in items if i.get("serial")}
    changes, skipped = [], []
    for item in sorted(items, key=lambda i: numbering.parse(i.get("serial")) or 0):
        old = item.get("serial")
        if not numbering.is_internal(old):
            continue
        new = numbering.normalize(old)
        if new == old:
            continue
        if new in taken:
            skipped.append((item["pk"], old, new))
            continue
        changes.append((item["pk"], old, new))
        taken.add(new)
    return changes, skipped


def main():
    ap = base_parser(__doc__)
    ap.add_argument("--prefix", default="P2PTD", help="префикс номеров (по умолчанию P2PTD)")
    ap.add_argument("--digits", type=int, default=4, help="цифр в номере (по умолчанию 4)")
    args = ap.parse_args()
    numbering = Numbering(args.prefix, args.digits)

    client = connect(args, ap)
    items = client.get_all(STOCK_API, search=args.prefix)
    changes, skipped = plan(items, numbering)

    for pk, old, new in changes:
        print(f"{'~' if args.dry_run else '+'} {old} → {new}  (позиция #{pk})")
        if not args.dry_run:
            client.request("PATCH", f"{STOCK_API}{pk}/", {"serial": new})
    for pk, old, new in skipped:
        print(f"! {old}: номер {new} уже занят другой позицией, оставлено как есть (позиция #{pk})")

    verb = "Будет переименовано" if args.dry_run else "Переименовано"
    print(f"\n{verb}: {len(changes)}, пропущено: {len(skipped)}.")
    if args.dry_run:
        print("Режим проверки: на сервер ничего не отправлялось.")


if __name__ == "__main__":
    main()
