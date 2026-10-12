#!/usr/bin/env python3
"""
Создание категорий деталей в InvenTree через API.

  1. Проверка без изменений:
       python create_categories.py --dry-run

  2. Реальное создание:
       python create_categories.py

Адрес и токен берутся из файла .env (см. .env.example).
Их можно передать и аргументами: --url https://... --token inv-xxxx
Без токена можно войти логином: --user admin (пароль спросит скрипт).

Скрипт можно запускать повторно: существующие категории пропускаются.
"""

from inventree_client import base_parser, connect

# Дерево категорий. Словарь = есть вложенные категории, список = конечные категории,
# пустой список = категория без вложенных.
CATEGORIES = {
    "Звук": {
        "Микшерные пульты": ["Цифровые", "Аналоговые"],
        "Микрофоны": ["Проводные", "Радиосистемы"],
        "Акустические системы": ["Активные", "Пассивные", "Трансляционные"],
        "Усилители мощности": [],
    },
    "DJ-оборудование": {
        "Проигрыватели": [],
        "Микшеры": [],
        "Контроллеры": [],
    },
    "Свет": {
        "Световые приборы": ["PAR", "WASH", "LEDBAR"],
        "Контроллеры": [],
        "Дым и эффекты": [],
    },
    "Видео": {
        "Проекторы": [],
        "Экраны": [],
        "Кронштейны": [],
        "Телевизоры": [],
    },
    "Сцена": {
        "Стойки и штативы": ["Микрофонные", "Акустические", "Световые"],
    },
    "IT и сеть": {
        "Компьютеры": [],
        "Коммутаторы": [],
        "Маршрутизаторы": [],
    },
    "Кабели и коммутация": {
        "Аудио": [],
        "Видео": [],
        "Силовые": [],
        "Сеть": [],
        "Переходники": [],
    },
    "Прочее": [],
}

API = "/api/part/category/"


def walk(tree, path=()):
    """Обход дерева: выдаёт (путь, имя) в порядке от родителей к детям."""
    if isinstance(tree, dict):
        items = [(name, sub) for name, sub in tree.items()]
    else:
        items = [(name, []) for name in tree]
    for name, sub in items:
        yield path, name
        yield from walk(sub, path + (name,))


def main():
    ap = base_parser(__doc__)
    args = ap.parse_args()

    entries = list(walk(CATEGORIES))

    if args.dry_run:
        print(f"Будет проверено {len(entries)} категорий:\n")
        for path, name in entries:
            print("  " * len(path) + ("└ " if path else "") + name)
        print("\nРежим проверки: на сервер ничего не отправлялось.")
        return

    client = connect(args, ap)
    existing = client.tree(API)
    ids = {(): None}
    created = skipped = 0

    for path, name in entries:
        parent_id = ids[path]
        key = (parent_id, name)
        label = " / ".join(path + (name,))
        if key in existing:
            ids[path + (name,)] = existing[key]
            skipped += 1
            print(f"= уже есть: {label}")
        else:
            pk = client.post(API, {"name": name, "parent": parent_id})["pk"]
            existing[key] = pk
            ids[path + (name,)] = pk
            created += 1
            print(f"+ создано:  {label}")

    print(f"\nГотово. Создано: {created}, пропущено (уже были): {skipped}, всего в списке: {len(entries)}.")


if __name__ == "__main__":
    main()
