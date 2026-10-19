#!/usr/bin/env python3
"""
Устанавливает в InvenTree шаблон этикетки P2PTD для позиций склада.

  1. Проверка (покажет, создаст шаблон или обновит):
       python install_label_template.py --dry-run

  2. Установка или обновление:
       python install_label_template.py

После этого этикетки печатаются прямо из InvenTree (позиция или несколько
позиций → «Печать этикеток» → шаблон «P2PTD 62x29»), а Telegram-бот
присылает этикетку, отрисованную InvenTree по этому же шаблону.

Другой размер ленты: --width 50 --height 25 --name "P2PTD 50x25"
(в боте укажите то же имя в BOT_LABEL_TEMPLATE).

Нужен токен пользователя со статусом staff (шаблоны меняют только они).
Скрипт можно запускать повторно: существующий шаблон с тем же именем обновляется.
"""

import sys
from pathlib import Path

from inventree_client import base_parser, connect

API = "/api/label/template/"
HERE = Path(__file__).resolve().parent


def main():
    ap = base_parser(__doc__)
    ap.add_argument("--name", default="P2PTD 62x29", help="имя шаблона в InvenTree")
    ap.add_argument("--width", type=float, default=62, help="ширина этикетки, мм")
    ap.add_argument("--height", type=float, default=29, help="высота этикетки, мм")
    ap.add_argument("--file", default=str(HERE / "labels" / "p2ptd_stockitem.html"), help="файл шаблона")
    args = ap.parse_args()

    template = Path(args.file).read_bytes()
    client = connect(args, ap)
    me = client.request("GET", "/api/user/me/")
    if not me.get("is_staff") and not me.get("is_superuser"):
        sys.exit(f"Пользователь {me.get('username')} не staff: шаблоны этикеток в InvenTree меняют только "
                 "сотрудники со статусом staff. Запустите скрипт с токеном администратора.")

    existing = next((t for t in client.get_all(API, model_type="stockitem") if t["name"] == args.name), None)
    fields = {
        "name": args.name,
        "description": "Этикетка P2PTD: QR, внутренний номер, модель, место",
        "model_type": "stockitem",
        "width": f"{args.width:g}",
        "height": f"{args.height:g}",
        "enabled": "true",
    }
    action = f"обновить шаблон #{existing['pk']}" if existing else "создать шаблон"
    print(f"{args.name}: {action}, {args.width:g} × {args.height:g} мм, файл {Path(args.file).name}")
    if args.dry_run:
        print("Режим проверки: на сервер ничего не отправлялось.")
        return

    files = {"template": ("p2ptd_stockitem.html", template)}
    if existing:
        result = client.request("PATCH", f"{API}{existing['pk']}/", fields, files=files)
    else:
        result = client.request("POST", API, fields, files=files)
    print(f"Готово: шаблон «{result['name']}» (#{result['pk']}). В боте: BOT_LABEL_TEMPLATE={result['name']}")


if __name__ == "__main__":
    main()
