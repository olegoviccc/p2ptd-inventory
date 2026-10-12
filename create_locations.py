#!/usr/bin/env python3
"""
Создание мест хранения в InvenTree через API.

Запуск (нужен только Python 3, ничего устанавливать не надо):

  1. Проверка без изменений:
       python create_locations.py --url https://ваш-адрес --user admin --dry-run

  2. Реальное создание:
       python create_locations.py --url https://ваш-адрес --user admin

Пароль спросит скрипт. Вместо логина/пароля можно передать токен:
       python create_locations.py --url https://ваш-адрес --token inv-xxxx

Скрипт можно запускать повторно: уже существующие места пропускаются.
"""

import argparse
import base64
import getpass
import json
import sys
import urllib.error
import urllib.request

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
        req = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method
        )
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
        """Все существующие места: {(parent_id, name): id}"""
        result = self.request("GET", API)
        items = result["results"] if isinstance(result, dict) else result
        return {(i.get("parent"), i["name"]): i["pk"] for i in items}

    def create(self, name, parent):
        res = self.request("POST", API, {"name": name, "parent": parent})
        return res["pk"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--url", required=True, help="адрес InvenTree, например https://inventree.example.com")
    ap.add_argument("--user", help="логин администратора")
    ap.add_argument("--token", help="API-токен (вместо логина и пароля)")
    ap.add_argument("--dry-run", action="store_true", help="только показать, что будет создано")
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

    if not args.token and not args.user:
        ap.error("укажите --user или --token")
    password = None if args.token else getpass.getpass("Пароль: ")
    client = Client(args.url, token=args.token, user=args.user, password=password)

    existing = client.existing()
    created = skipped = 0

    for parent_name, children in LOCATIONS.items():
        key = (None, parent_name)
        if key in existing:
            parent_id = existing[key]
            skipped += 1
            print(f"= уже есть: {parent_name}")
        else:
            parent_id = client.create(parent_name, None)
            existing[key] = parent_id
            created += 1
            print(f"+ создано:  {parent_name}")

        for child in children:
            ckey = (parent_id, child)
            if ckey in existing:
                skipped += 1
                print(f"    = уже есть: {parent_name} / {child}")
            else:
                existing[ckey] = client.create(child, parent_id)
                created += 1
                print(f"    + создано:  {parent_name} / {child}")

    print(f"\nГотово. Создано: {created}, пропущено (уже были): {skipped}, всего в списке: {total}.")


if __name__ == "__main__":
    main()
