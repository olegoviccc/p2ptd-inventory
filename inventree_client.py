"""
Общий клиент InvenTree API для скриптов этого репозитория.

Адрес и токен берутся (по приоритету):
  1. из аргументов --url / --token / --user;
  2. из переменных окружения INVENTREE_URL / INVENTREE_TOKEN;
  3. из файла .env рядом со скриптами (см. .env.example).

Файл .env лежит только у вас на компьютере и в git не попадает (.gitignore).
"""

import argparse
import base64
import getpass
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent / ".env"


def find_env_file():
    """.env рядом со скриптами. Windows часто сохраняет его как .env.txt."""
    for path in (ENV_FILE, ENV_FILE.with_name(".env.txt")):
        if path.exists():
            return path
    return None


def load_env():
    """Читает KEY=VALUE из .env в os.environ (не перезаписывая уже заданное)."""
    path = find_env_file()
    if not path:
        return
    # utf-8-sig: Блокнот Windows ставит в начало файла невидимый BOM
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):]
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env_hint():
    """Пояснение, почему адрес или токен не нашлись в .env."""
    path = find_env_file()
    if not path:
        return f"файл {ENV_FILE} не найден (он должен лежать рядом со скриптами)"
    keys = [
        line.split("=", 1)[0].strip()
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if "=" in line and not line.strip().startswith("#")
    ]
    return f"в файле {path} найдены ключи: {', '.join(keys) or 'никаких'}"


def add_connection_args(ap):
    ap.add_argument("--url", help="адрес InvenTree (по умолчанию INVENTREE_URL из .env)")
    ap.add_argument("--token", help="API-токен (по умолчанию INVENTREE_TOKEN из .env)")
    ap.add_argument("--user", help="логин, если токена нет (пароль спросит скрипт)")


def connect(args, ap):
    """Создаёт Client по аргументам командной строки и .env."""
    load_env()
    url = args.url or os.environ.get("INVENTREE_URL")
    token = args.token or os.environ.get("INVENTREE_TOKEN")
    if not url:
        ap.error("не задан адрес: укажите --url или INVENTREE_URL в .env; " + env_hint())
    if token:
        return Client(url, token=token)
    if not args.user:
        ap.error("нет токена: укажите INVENTREE_TOKEN в .env, --token или --user; " + env_hint())
    return Client(url, user=args.user, password=getpass.getpass("Пароль: "))


class Client:
    def __init__(self, base_url, token=None, user=None, password=None):
        self.base = base_url.rstrip("/")
        if token:
            self.auth = "Token " + token
        else:
            raw = f"{user}:{password}".encode("utf-8")
            self.auth = "Basic " + base64.b64encode(raw).decode("ascii")

    def request(self, method, path, payload=None, params=None):
        url = path if path.startswith("http") else self.base + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        data = None
        headers = {"Authorization": self.auth, "Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            sys.exit(f"Ошибка API {e.code} на {method} {url}: {detail}")
        except urllib.error.URLError as e:
            sys.exit(f"Не удалось подключиться к {self.base}: {e.reason}")

    def get_all(self, path, **params):
        """Все записи списка с учётом постраничной выдачи."""
        params = {"limit": 500, **params}
        result = self.request("GET", path, params=params)
        if isinstance(result, list):
            return result
        items = list(result["results"])
        while result.get("next"):
            result = self.request("GET", result["next"])
            items.extend(result["results"])
        return items

    def post(self, path, payload):
        return self.request("POST", path, payload)

    def tree(self, path):
        """Дерево (категории или места): {(parent_id, name): pk}."""
        return {(i.get("parent"), i["name"]): i["pk"] for i in self.get_all(path)}


def resolve_path(tree, names):
    """Находит pk узла по цепочке имён от корня, None если нет."""
    pk = None
    for name in names:
        pk = tree.get((pk, name))
        if pk is None:
            return None
    return pk


def base_parser(doc):
    ap = argparse.ArgumentParser(description=doc, formatter_class=argparse.RawTextHelpFormatter)
    add_connection_args(ap)
    ap.add_argument("--dry-run", action="store_true", help="только показать, что будет сделано")
    return ap
