"""Поддельный InvenTree API для тестов (в памяти, в отдельном потоке)."""

import base64
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TOKEN = "test-token"


class FakeInvenTree:
    def __init__(self):
        self.db = {k: [] for k in ("category", "location", "part", "company", "mpn", "stock", "attachment",
                                   "label", "output")}
        self.printed = []  # вызовы /api/label/print/
        self.removed = []  # вызовы /api/stock/remove/
        self.transfers = []  # вызовы /api/stock/transfer/
        self.part_images = {}
        self.requests = 0
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def authorized(self):
                auth = self.headers.get("Authorization", "")
                if auth == f"Token {TOKEN}":
                    return True
                if auth.startswith("Basic "):
                    return base64.b64decode(auth[6:]).decode() == "anna:secret"
                return False

            def body(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                ctype = self.headers.get("Content-Type", "")
                if ctype.startswith("multipart/"):
                    boundary = ctype.split("boundary=")[1].encode()
                    fields = {}
                    for chunk in raw.split(b"--" + boundary)[1:-1]:
                        head, _, data = chunk.strip(b"\r\n").partition(b"\r\n\r\n")
                        name = re.search(rb'name="([^"]+)"', head).group(1).decode()
                        filename = re.search(rb'filename="([^"]*)"', head)
                        fields[name] = data if filename else data.decode()
                        if filename:
                            fields[name + "__filename"] = filename.group(1).decode()
                    return fields
                return json.loads(raw) if raw else {}

            def do_GET(self):
                server.requests += 1
                if not self.authorized():
                    return self.send({"detail": "auth"}, 401)
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                path = u.path
                if path == "/api/user/token/":
                    return self.send({"token": TOKEN})
                if path == "/api/user/roles/":
                    return self.send({"is_superuser": True, "roles": {}})
                if path == "/api/":
                    return self.send({"server": "InvenTree", "version": "1.1.0", "apiVersion": 400})
                if path == "/api/user/me/":
                    return self.send({"username": "admin", "is_staff": True})
                if path.startswith("/media/"):
                    body = b"%PDF-1.7 fake label"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/pdf")
                    self.end_headers()
                    return self.wfile.write(body)
                m = re.match(r"/api/data-output/(\d+)/$", path)
                if m:  # как фоновая задача InvenTree: со второго запроса результат готов
                    out = server.get("output", int(m.group(1)))
                    out.update(complete=True, output=f"/media/data_output/label_{out['pk']}.pdf")
                    return self.send(out)
                if path == "/api/label/template/":
                    items = [{k: v for k, v in t.items() if k != "template"} for t in server.db["label"]
                             if t["model_type"] == q.get("model_type", t["model_type"])]
                    return self.send({"count": len(items), "next": None, "results": items})
                m = re.match(r"/api/(part|stock)/(\d+)/$", path)
                if m:
                    item = server.get(m.group(1), int(m.group(2)))
                    return self.send(server.detail(item) if m.group(1) == "stock" else item)
                table = {
                    "/api/part/category/": "category", "/api/stock/location/": "location",
                    "/api/part/": "part", "/api/company/": "company",
                    "/api/company/part/manufacturer/": "mpn", "/api/stock/": "stock",
                    "/api/attachment/": "attachment",
                }[path]
                items = list(server.db[table])
                for key in ("part", "location", "serial", "batch", "model_type", "model_id"):
                    if key in q:
                        items = [i for i in items if str(i.get(key)) == q[key]]
                if "search" in q:
                    s = q["search"].casefold()
                    items = [i for i in items if any(s in str(v).casefold() for v in i.values())]
                if table == "stock":
                    items = [server.detail(i) for i in items]
                # файлы отдаются ссылкой, как в InvenTree, а не байтами
                items = [{k: (f"/media/{k}/{i.get(k + '__filename', 'file')}" if isinstance(v, bytes) else v)
                          for k, v in i.items() if not k.endswith("__filename")} for i in items]
                self.send({"count": len(items), "next": None, "results": items})

            def do_POST(self):
                server.requests += 1
                if not self.authorized():
                    return self.send({"detail": "auth"}, 401)
                d = self.body()
                table = {
                    "/api/part/": "part", "/api/company/": "company",
                    "/api/company/part/manufacturer/": "mpn", "/api/stock/": "stock",
                    "/api/attachment/": "attachment",
                }.get(self.path)
                if self.path == "/api/label/template/":
                    added = server.add("label", d)
                    return self.send({k: v for k, v in added.items() if k != "template"}, 201)
                if self.path == "/api/label/print/":
                    if not any(t["pk"] == d["template"] for t in server.db["label"]):
                        return self.send({"template": ["Invalid pk"]}, 400)
                    server.printed.append(d)
                    return self.send(server.add("output", {"complete": False, "output": None, "errors": None}), 201)
                if self.path == "/api/stock/transfer/":
                    for row in d["items"]:
                        server.get("stock", row["pk"])["location"] = d["location"]
                    server.transfers.append(d)
                    return self.send({})
                if self.path == "/api/stock/remove/":
                    for row in d["items"]:
                        item = server.get("stock", row["pk"])
                        item["quantity"] -= row["quantity"]
                        server.removed.append(row)
                        if item["quantity"] <= 0:
                            server.db["stock"].remove(item)
                    return self.send({})
                if table == "mpn":
                    part = server.get("part", d["part"])
                    if not part.get("purchaseable"):
                        return self.send({"part": ["Select a valid choice."]}, 400)
                if table == "stock":
                    d.setdefault("serial", None)
                    d.setdefault("batch", None)
                    d.setdefault("notes", "")
                added = server.add(table, d)
                self.send({k: v for k, v in added.items() if not isinstance(v, bytes)}, 201)

            def do_PATCH(self):
                server.requests += 1
                if not self.authorized():
                    return self.send({"detail": "auth"}, 401)
                m = re.match(r"/api/(part|stock|label/template)/(\d+)/$", self.path)
                kind, pk = m.group(1), int(m.group(2))
                d = self.body()
                if kind == "label/template":
                    server.get("label", pk).update(d)
                    return self.send({k: v for k, v in server.get("label", pk).items() if k != "template"})
                if "image" in d:
                    server.part_images[pk] = d.pop("image")
                    d["image"] = f"/media/part_images/part_{pk}.jpg"
                if kind == "stock" and d.get("serial"):
                    if any(i.get("serial") == d["serial"] and i["pk"] != pk for i in server.db["stock"]):
                        return self.send({"serial": ["Serial number already exists"]}, 400)
                item = server.get(kind, pk)
                item.update(d)
                self.send(item)

            def do_DELETE(self):
                server.requests += 1
                m = re.match(r"/api/stock/(\d+)/$", self.path)
                server.db["stock"].remove(server.get("stock", int(m.group(1))))
                self.send_response(204)  # как настоящий InvenTree: без тела
                self.end_headers()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def add(self, table, d):
        d = dict(d)
        d["pk"] = max((i["pk"] for i in self.db[table]), default=0) + 1
        self.db[table].append(d)
        return d

    def get(self, kind, pk):
        return next(i for i in self.db[kind] if i["pk"] == pk)

    def detail(self, item):
        part = self.get("part", item["part"])
        loc = next((l for l in self.db["location"] if l["pk"] == item.get("location")), None)
        return {**item, "part_detail": {"name": part["name"], "full_name": part["name"]},
                "location_detail": {"pathstring": loc["name"] if loc else ""}}

    def close(self):
        self.httpd.shutdown()


def seed(s):
    """Каталог и склад, как после импорта райдера Пешки."""
    for pk, name, parent in [(1, "DJ-оборудование", None), (2, "Проигрыватели", 1), (3, "Микшеры", 1)]:
        s.db["category"].append({"pk": pk, "name": name, "parent": parent})
    s.db["location"] += [{"pk": 10, "name": "Пешка", "parent": None},
                         {"pk": 11, "name": "Диджейка", "parent": 10}]
    s.db["part"].append({"pk": 1, "name": "Pioneer DJ XDJ-1000MK2", "category": 2,
                         "trackable": False, "purchaseable": True, "image": None})
    s.db["mpn"].append({"pk": 1, "part": 1, "manufacturer": 1, "MPN": "XDJ-1000MK2"})
    # позиция из райдера: 2 шт. без серийника
    s.db["stock"].append({"pk": 1, "part": 1, "location": 11, "quantity": 2, "serial": None, "batch": None, "notes": ""})
    # уже выданный номер у другого прибора
    s.db["stock"].append({"pk": 2, "part": 1, "location": 10, "quantity": 1, "serial": "P2PTD-00007", "batch": "X1",
                        "notes": ""})
