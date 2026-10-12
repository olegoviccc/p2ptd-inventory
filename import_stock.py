#!/usr/bin/env python3
"""
Загрузка оборудования из data/riders/*.yaml в InvenTree.

Для каждой позиции скрипт:
  * создаёт производителя (компания с флагом «Производитель»), если его нет;
  * создаёт деталь в нужной категории и привязку к модели производителя (MPN);
  * создаёт позицию на складе в месте хранения со статусом
    «Требует внимания» и примечанием «из райдера, не проверено».

  1. Проверка без изменений (сервер не нужен):
       python import_stock.py --dry-run

  2. Загрузка всех заведений или только нужных файлов:
       python import_stock.py
       python import_stock.py data/riders/peshka.yaml

Адрес и токен берутся из .env (см. .env.example). Нужен PyYAML: pip install pyyaml

Скрипт можно запускать повторно. Существующие производители и детали
переиспользуются. Позиция склада не создаётся, если у этой детали уже есть
позиция ровно в этом месте (например, уже проверенная и с серийниками).
"""

import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("Нужен PyYAML: pip install pyyaml")

from create_categories import CATEGORIES, walk
from create_locations import LOCATIONS
from inventree_client import base_parser, connect, resolve_path

DATA_DIR = Path(__file__).resolve().parent / "data" / "riders"

STATUS = {"attention": 50, "ok": 10}  # коды StockStatus в InvenTree

CATEGORY_API = "/api/part/category/"
LOCATION_API = "/api/stock/location/"
PART_API = "/api/part/"
COMPANY_API = "/api/company/"
MPN_API = "/api/company/part/manufacturer/"
STOCK_API = "/api/stock/"


def split_path(path):
    return tuple(p.strip() for p in str(path).split("/") if p.strip())


def load(files):
    """Читает YAML и разворачивает позиции: одна запись = (заведение, место, деталь, кол-во)."""
    known_categories = {path + (name,) for path, name in walk(CATEGORIES)}
    parts, entries, errors = {}, [], []

    for f in files:
        doc = yaml.safe_load(Path(f).read_text(encoding="utf-8"))
        venue = doc["venue"]
        if venue not in LOCATIONS:
            errors.append(f"{f}: заведения «{venue}» нет в create_locations.py")
            continue
        for i, item in enumerate(doc.get("items") or [], 1):
            where = f"{f} #{i} ({item.get('name')})"
            name = str(item.get("name", "")).strip()
            category = split_path(item.get("category", ""))
            if not name:
                errors.append(f"{where}: нет name")
                continue
            if category not in known_categories:
                errors.append(f"{where}: категории «{' / '.join(category)}» нет в дереве")
            qty = item.get("qty")
            if not isinstance(qty, (int, float)) or qty <= 0:
                errors.append(f"{where}: qty должно быть положительным числом")

            manufacturer = (item.get("manufacturer") or "").strip()
            part = {
                "name": name,
                "category": category,
                "manufacturer": manufacturer,
                "model": str(item.get("model") or "").strip(),
                "trackable": bool(item.get("trackable", bool(manufacturer))),
            }
            if name in parts and parts[name] != part:
                errors.append(f"{where}: деталь «{name}» описана иначе, чем в другом месте")
            parts.setdefault(name, part)

            locations = item.get("locations") or [item.get("location") or ""]
            for loc in locations:
                loc = str(loc).strip()
                if loc and loc not in LOCATIONS[venue]:
                    errors.append(f"{where}: места «{loc}» нет у заведения «{venue}»")
                entries.append({
                    "venue": venue,
                    "location": loc,
                    "part": name,
                    "qty": qty,
                    "note": item.get("note"),
                    "check": item.get("check"),
                    "source": doc.get("source", Path(f).name),
                })

    seen = set()
    for e in entries:
        key = (e["venue"], e["location"], e["part"])
        if key in seen:
            errors.append(f"«{e['part']}» дважды в {' / '.join(filter(None, key[:2]))}: объедините в одну позицию")
        seen.add(key)

    return parts, entries, errors


def stock_notes(e):
    lines = [f"Из райдера {e['source']}, не проверено при обходе."]
    if e["note"]:
        lines.append(f"Примечание: {e['note']}")
    if e["check"]:
        lines.append(f"Проверить: {e['check']}")
    return "\n\n".join(lines)


def location_label(e):
    return " / ".join(filter(None, (e["venue"], e["location"])))


def print_plan(parts, entries):
    for venue in dict.fromkeys(e["venue"] for e in entries):
        print(venue)
        for e in (e for e in entries if e["venue"] == venue):
            loc = e["location"] or "(само заведение)"
            mark = "  ⚠ " + e["check"] if e["check"] else ""
            print(f"  {loc:<16} {e['qty']:>3} × {e['part']}{mark}")
        print()
    manufacturers = {p["manufacturer"] for p in parts.values() if p["manufacturer"]}
    print(f"Итого: {len(manufacturers)} производителей, {len(parts)} деталей, "
          f"{len(entries)} позиций склада, {sum(e['qty'] for e in entries):g} единиц.")


# Какие права группы пользователя нужны (Админка → Группы → правила).
# Производители (Company) и их модели в InvenTree относятся к правилу Purchase Order.
REQUIRED_ROLES = {
    "part_category": ["view"],
    "part": ["view", "add", "change"],
    "stock_location": ["view"],
    "stock": ["view", "add"],
    "purchase_order": ["view", "add"],
}


def check_roles(client):
    """Проверяет права пользователя до любых изменений."""
    info = client.request("GET", "/api/user/roles/")
    if info.get("is_superuser"):
        return
    roles = info.get("roles") or {}
    missing = [
        f"{role}: {perm}"
        for role, perms in REQUIRED_ROLES.items()
        for perm in perms
        if perm not in (roles.get(role) or [])
    ]
    if missing:
        sys.exit(
            f"У пользователя {info.get('username', '')} не хватает прав:\n  "
            + "\n  ".join(missing)
            + "\n\nДобавьте их группе пользователя: Админка → Группы → правила (Rule sets)."
            "\nПроизводители в InvenTree относятся к правилу Purchase Order."
        )


class Importer:
    def __init__(self, client, status):
        self.c = client
        self.status = status
        self.categories = client.tree(CATEGORY_API)
        self.locations = client.tree(LOCATION_API)
        self.parts = {p["name"]: p for p in client.get_all(PART_API)}
        self.companies = {c["name"].casefold(): c["pk"] for c in client.get_all(COMPANY_API)}
        # Все привязки к моделям одним запросом: фильтр ?part= отвергает
        # детали без флага «Покупная» ответом 400
        self.mpns = {(m["part"], m["manufacturer"], m["MPN"]) for m in client.get_all(MPN_API)}
        self.stats = dict.fromkeys(["companies", "parts", "mpn", "stock", "stock_skipped"], 0)

    def manufacturer(self, name):
        key = name.casefold()
        if key not in self.companies:
            res = self.c.post(COMPANY_API, {
                "name": name, "is_manufacturer": True, "is_supplier": False, "is_customer": False,
            })
            self.companies[key] = res["pk"]
            self.stats["companies"] += 1
            print(f"+ производитель: {name}")
        return self.companies[key]

    def part(self, spec):
        existing = self.parts.get(spec["name"])
        if existing:
            pk = existing["pk"]
            if spec["model"] and not existing.get("purchaseable"):
                # Модель производителя (MPN) можно привязать только к покупной детали
                self.c.request("PATCH", f"{PART_API}{pk}/", {"purchaseable": True})
                existing["purchaseable"] = True
        else:
            category = resolve_path(self.categories, spec["category"])
            if category is None:
                sys.exit(f"В InvenTree нет категории {' / '.join(spec['category'])}: "
                         "сначала запустите create_categories.py")
            res = self.c.post(PART_API, {
                "name": spec["name"],
                "description": spec["name"],
                "category": category,
                "trackable": spec["trackable"],
                "purchaseable": True,
                "active": True,
            })
            pk = res["pk"]
            self.parts[spec["name"]] = res
            self.stats["parts"] += 1
            print(f"+ деталь: {spec['name']}")

        if spec["manufacturer"] and spec["model"]:
            mfr = self.manufacturer(spec["manufacturer"])
            if (pk, mfr, spec["model"]) not in self.mpns:
                self.c.post(MPN_API, {"part": pk, "manufacturer": mfr, "MPN": spec["model"]})
                self.mpns.add((pk, mfr, spec["model"]))
                self.stats["mpn"] += 1
        elif spec["manufacturer"]:
            self.manufacturer(spec["manufacturer"])
        return pk

    def stock(self, e, part_pk):
        names = (e["venue"],) + ((e["location"],) if e["location"] else ())
        loc = resolve_path(self.locations, names)
        if loc is None:
            sys.exit(f"В InvenTree нет места {' / '.join(names)}: сначала запустите create_locations.py")
        label = location_label(e)
        if self.c.get_all(STOCK_API, part=part_pk, location=loc, cascade="false"):
            self.stats["stock_skipped"] += 1
            print(f"= уже есть: {label}: {e['part']}")
            return
        self.c.post(STOCK_API, {
            "part": part_pk,
            "location": loc,
            "quantity": e["qty"],
            "status": self.status,
            "notes": stock_notes(e),
        })
        self.stats["stock"] += 1
        print(f"+ склад: {label}: {e['qty']} × {e['part']}")


def main():
    ap = base_parser(__doc__)
    ap.add_argument("files", nargs="*", help="YAML-файлы (по умолчанию все из data/riders)")
    ap.add_argument("--status", choices=STATUS, default="attention",
                    help="статус новых позиций (по умолчанию attention = «Требует внимания»)")
    args = ap.parse_args()

    files = args.files or sorted(DATA_DIR.glob("*.yaml"))
    if not files:
        sys.exit(f"Нет YAML-файлов в {DATA_DIR}")

    parts, entries, errors = load(files)
    if errors:
        print("Ошибки в данных, исправьте YAML:\n  " + "\n  ".join(errors))
        sys.exit(1)

    if args.dry_run:
        print_plan(parts, entries)
        print("\nРежим проверки: на сервер ничего не отправлялось.")
        return

    client = connect(args, ap)
    check_roles(client)
    importer = Importer(client, STATUS[args.status])
    part_pks = {name: importer.part(spec) for name, spec in parts.items()}
    for e in entries:
        importer.stock(e, part_pks[e["part"]])

    s = importer.stats
    print(f"\nГотово. Производителей: +{s['companies']}, деталей: +{s['parts']}, "
          f"моделей (MPN): +{s['mpn']}, позиций склада: +{s['stock']}, "
          f"пропущено (уже были): {s['stock_skipped']}.")


if __name__ == "__main__":
    main()
