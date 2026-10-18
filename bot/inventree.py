"""Асинхронный клиент InvenTree API для бота.

В отличие от inventree_client.Client (для разовых скриптов), ошибки здесь
не завершают процесс, а поднимаются как InvenTreeError, а запросы уходят
в отдельный поток, чтобы не блокировать бота.
"""

import asyncio
import base64
import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
import uuid

STATUS_OK = 10
STATUS_ATTENTION = 50


class InvenTreeError(Exception):
    def __init__(self, status, detail):
        super().__init__(f"InvenTree {status}: {detail}")
        self.status = status
        self.detail = detail


def _multipart(fields, files):
    boundary = uuid.uuid4().hex
    out = []
    for name, value in fields.items():
        out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, (filename, data) in files.items():
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        out.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n".encode() + data + b"\r\n"
        )
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


class InvenTree:
    def __init__(self, base_url, token=None, basic=None):
        self.base = base_url.rstrip("/")
        if token:
            self.auth = "Token " + token
        else:
            user, password = basic
            self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    # --- транспорт

    def _request(self, method, path, payload=None, params=None, files=None):
        url = path if path.startswith("http") else self.base + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        headers = {"Authorization": self.auth, "Accept": "application/json"}
        data = None
        if files:
            data, headers["Content-Type"] = _multipart(payload or {}, files)
        elif payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as e:
            raise InvenTreeError(e.code, e.read().decode(errors="replace")[:500]) from None
        except urllib.error.URLError as e:
            raise InvenTreeError(0, f"нет связи с {self.base}: {e.reason}") from None

    async def call(self, method, path, payload=None, params=None, files=None):
        return await asyncio.to_thread(self._request, method, path, payload, params, files)

    async def get(self, path, **params):
        return await self.call("GET", path, params=params or None)

    async def post(self, path, payload):
        return await self.call("POST", path, payload)

    async def patch(self, path, payload, files=None):
        return await self.call("PATCH", path, payload, files=files)

    async def get_all(self, path, **params):
        result = await self.get(path, limit=500, **params)
        if isinstance(result, list):
            return result
        items = list(result["results"])
        while result.get("next"):
            result = await self.call("GET", result["next"])
            items.extend(result["results"])
        return items

    # --- пользователь

    async def obtain_token(self, name="telegram-bot"):
        """Личный API-токен пользователя (клиент должен быть создан с basic=)."""
        res = await self.get("/api/user/token/", name=name)
        return res["token"]

    async def me(self):
        return await self.get("/api/user/me/")

    async def roles(self):
        return await self.get("/api/user/roles/")

    def web_url(self, kind, pk):
        return f"{self.base}/web/{kind}/{pk}"

    # --- справочники

    async def locations(self):
        return await self.get_all("/api/stock/location/")

    async def categories(self):
        return await self.get_all("/api/part/category/")

    async def parts(self):
        return await self.get_all("/api/part/")

    async def manufacturer_parts(self):
        return await self.get_all("/api/company/part/manufacturer/")

    async def companies(self):
        return await self.get_all("/api/company/")

    # --- детали

    async def get_or_create_manufacturer(self, name):
        for c in await self.get_all("/api/company/", search=name):
            if c["name"].casefold() == name.casefold():
                return c["pk"]
        res = await self.post("/api/company/", {
            "name": name, "is_manufacturer": True, "is_supplier": False, "is_customer": False,
        })
        return res["pk"]

    async def create_part(self, name, category, manufacturer=None, model=None):
        part = await self.post("/api/part/", {
            "name": name,
            "description": name,
            "category": category,
            "trackable": True,
            "purchaseable": True,
            "active": True,
        })
        if manufacturer and model:
            mfr = await self.get_or_create_manufacturer(manufacturer)
            await self.post("/api/company/part/manufacturer/", {"part": part["pk"], "manufacturer": mfr, "MPN": model})
        return part

    async def ensure_trackable(self, part):
        """Серийник можно присвоить только отслеживаемой детали."""
        if not part.get("trackable"):
            await self.patch(f"/api/part/{part['pk']}/", {"trackable": True})
            part["trackable"] = True

    async def set_part_image(self, part_pk, filename, data):
        await self.patch(f"/api/part/{part_pk}/", {}, files={"image": (filename, data)})

    # --- склад

    async def unverified_stock(self, part_pk, location_pk):
        """Позиции без серийника в этом месте (например, из райдера)."""
        items = await self.get_all("/api/stock/", part=part_pk, location=location_pk, cascade="false")
        return [i for i in items if not i.get("serial") and float(i.get("quantity", 0)) >= 1]

    DETAIL = {"part_detail": "true", "location_detail": "true"}

    async def find_by_serial(self, serial):
        items = await self.get_all("/api/stock/", serial=serial, **self.DETAIL)
        return items[0] if items else None

    async def find_by_batch(self, batch, part_pk=None):
        params = {"batch": batch, **self.DETAIL}
        if part_pk:
            params["part"] = part_pk
        return await self.get_all("/api/stock/", **params)

    async def search_stock(self, text):
        return await self.get_all("/api/stock/", search=text, **self.DETAIL)

    async def stock_item(self, pk):
        return await self.get(f"/api/stock/{pk}/", **self.DETAIL)

    async def location(self, pk):
        return await self.get(f"/api/stock/location/{pk}/")

    async def create_stock(self, part_pk, location_pk, notes):
        res = await self.post("/api/stock/", {
            "part": part_pk, "location": location_pk, "quantity": 1,
            "status": STATUS_OK, "notes": notes,
        })
        return res[0] if isinstance(res, list) else res

    async def set_serial(self, stock_pk, serial, batch=None):
        payload = {"serial": serial}
        if batch:
            payload["batch"] = batch
        return await self.patch(f"/api/stock/{stock_pk}/", payload)

    async def delete_stock(self, stock_pk):
        await self.call("DELETE", f"/api/stock/{stock_pk}/")

    async def take_one(self, stock_pk, note):
        """Списывает 1 шт. с позиции (например, из райдера после сериализации)."""
        await self.post("/api/stock/remove/", {"items": [{"pk": stock_pk, "quantity": 1}], "notes": note})

    async def attach(self, model_type, model_id, filename, data, comment=""):
        await self.call("POST", "/api/attachment/", {
            "model_type": model_type, "model_id": model_id, "comment": comment,
        }, files={"attachment": (filename, data)})
