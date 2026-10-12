#!/usr/bin/env python3
"""
Создание категорий деталей в InvenTree через API.

  1. Проверка без изменений:
       python create_categories.py --url https://p2ptd-inventory.online --dry-run

  2. Реальное создание (пароль спросит скрипт):
       python create_categories.py --url https://p2ptd-inventory.online --user ваш_логин

Вместо логина и пароля можно передать токен: --token inv-xxxx
Скрипт можно запускать повторно: существующие категории пропускаются.
"""

import argparse
import base64
import getpass
import json
import sys
import urllib.error
import urllib.request

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


class Client:
    def __init__(self, base_url, token=None, user=None, password=None):
        self.base = base_url.rstrip("/")
        if token:
            self.auth = "Token " + token
        else:
            raw = f"{user}:{password}".encode("utf-8")
            self.auth = "Basic " + base64.b64encode(raw).decode("ascii")

    def request(self, method, path, payload=None):
        data = None
        headers = {"Authorization": self.auth, "Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            sys.exit(f"Ошибка API {e.code} на {method} {path}: {detail}")
        except urllib.error.URLError as e:
            sys.exit(f"Не удалось подключиться к {self.base}: {e.reason}")

    def existing(self):
        """Существующие категории: {(parent_id, name): id}"""
        result = self.request("GET", API)
        items = result["results"] if isinstance(result, dict) else result
        return {(i.get("parent"), i["name"]): i["pk"] for i in items}

    def create(self, name, parent):
        return self.request("POST", API, {"name": name, "parent": parent})["pk"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--url", required=True)
    ap.add_argument("--user")
    ap.add_argument("--token")
    ap.add_argument("--dry-run", action="store_true", help="только показать дерево")
    args = ap.parse_args()

    entries = list(walk(CATEGORIES))

    if args.dry_run:
        print(f"Будет проверено {len(entries)} категорий:\n")
        for path, name in entries:
            print("  " * len(path) + ("└ " if path else "") + name)
        print("\nРежим проверки: на сервер ничего не отправлялось.")
        return

    if not args.token and not args.user:
        ap.error("укажите --user или --token")
    password = None if args.token else getpass.getpass("Пароль: ")
    client = Client(args.url, token=args.token, user=args.user, password=password)

    existing = client.existing()
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
            pk = client.create(name, parent_id)
            existing[key] = pk
            ids[path + (name,)] = pk
            created += 1
            print(f"+ создано:  {label}")

    print(f"\nГотово. Создано: {created}, пропущено (уже были): {skipped}, всего в списке: {len(entries)}.")


if __name__ == "__main__":
    main()
