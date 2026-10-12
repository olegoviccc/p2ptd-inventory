#!/usr/bin/env python3
"""
Создание мест хранения в InvenTree через API.

  1. Проверка без изменений:
       python create_locations.py --dry-run

  2. Реальное создание:
       python create_locations.py

Адрес и токен берутся из файла .env (см. .env.example).
Их можно передать и аргументами: --url https://... --token inv-xxxx
Без токена можно войти логином: --user admin (пароль спросит скрипт).

Скрипт можно запускать повторно: уже существующие места пропускаются.
"""

from inventree_client import base_parser, connect

# Верхний уровень: заведение -> список внутренних локаций
LOCATIONS = {
    "Ровесник": ["Офис", "Сцена 2", "Синий шкаф", "Белый шкаф"],
    "Дорожка": ["Склад", "Рэк", "Зал", "Боулинг"],
    "Скрепка": ["Зал", "Двор", "Бэк"],
    "Пешка": ["Большой зал", "Малый зал", "Диджейка", "Бэк"],
    "Номер": [
        "Комната 1",
        "Комната 2",
        "Комната 3",
        "Комната 4",
        "Комната 5",
        "Бэк-лобби",
        "Лобби",
    ],
    "Гараж": ["123", "31", "105"],
}

API = "/api/stock/location/"


def main():
    ap = base_parser(__doc__)
    args = ap.parse_args()

    total = sum(1 + len(v) for v in LOCATIONS.values())

    if args.dry_run:
        print(f"Будет проверено {total} мест ({len(LOCATIONS)} заведений):\n")
        for parent, children in LOCATIONS.items():
            print(parent)
            for c in children:
                print(f"  └ {c}")
        print("\nРежим проверки: на сервер ничего не отправлялось.")
        return

    client = connect(args, ap)
    existing = client.tree(API)
    created = skipped = 0

    for parent_name, children in LOCATIONS.items():
        key = (None, parent_name)
        if key in existing:
            parent_id = existing[key]
            skipped += 1
            print(f"= уже есть: {parent_name}")
        else:
            parent_id = client.post(API, {"name": parent_name, "parent": None})["pk"]
            existing[key] = parent_id
            created += 1
            print(f"+ создано:  {parent_name}")

        for child in children:
            ckey = (parent_id, child)
            if ckey in existing:
                skipped += 1
                print(f"    = уже есть: {parent_name} / {child}")
            else:
                existing[ckey] = client.post(API, {"name": child, "parent": parent_id})["pk"]
                created += 1
                print(f"    + создано:  {parent_name} / {child}")

    print(f"\nГотово. Создано: {created}, пропущено (уже были): {skipped}, всего в списке: {total}.")


if __name__ == "__main__":
    main()
