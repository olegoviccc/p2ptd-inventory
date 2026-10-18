"""Асинхронный клиент InvenTree API для бота.

Одна aiohttp-сессия на весь бот: соединения с сервером переиспользуются
(без нового TLS-рукопожатия на каждый запрос). Ошибки не завершают процесс,
а поднимаются как InvenTreeError.
"""

import asyncio
import base64
import logging
import mimetypes
import time

import aiohttp

log = logging.getLogger(__name__)

STATUS_OK = 10
STOCK_DETAIL = {"part_detail": "true", "location_detail": "true"}


class InvenTreeError(Exception):
    def __init__(self, status, detail):
        super().__init__(f"InvenTree {status}: {detail}")
        self.status = status
        self.detail = detail


class Api:
    """Общий транспорт: базовый URL и пул соединений."""

    def __init__(self, base_url, timeout=60):
        self.base = base_url.rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session = None

    @property
    def session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def client(self, token):
        return InvenTree(self, "Token " + token)

    def basic(self, username, password):
        raw = base64.b64encode(f"{username}:{password}".encode()).decode()
        return InvenTree(self, "Basic " + raw)


class InvenTree:
    """Клиент от имени конкретного пользователя."""

    def __init__(self, api, auth):
        self.api = api
        self.auth = auth

    def web_url(self, kind, pk):
        return f"{self.api.base}/web/{kind}/{pk}"

    # --- транспорт

    async def call(self, method, path, payload=None, params=None, files=None):
        url = path if path.startswith("http") else self.api.base + path
        kwargs = {"params": params} if params else {}
        if files:
            form = aiohttp.FormData()
            for key, value in (payload or {}).items():
                form.add_field(key, str(value))
            for key, (filename, data) in files.items():
                form.add_field(key, data, filename=filename,
                               content_type=mimetypes.guess_type(filename)[0] or "application/octet-stream")
            kwargs["data"] = form
        elif payload is not None:
            kwargs["json"] = payload
        started = time.monotonic()
        try:
            async with self.api.session.request(
                method, url, headers={"Authorization": self.auth, "Accept": "application/json"}, **kwargs
            ) as resp:
                body = await resp.text()
                log.debug("%s %s → %s за %.0f мс", method, path, resp.status, (time.monotonic() - started) * 1000)
                if resp.status >= 400:
                    raise InvenTreeError(resp.status, body[:500])
                return await resp.json(content_type=None) if body else None
        except aiohttp.ClientError as e:
            raise InvenTreeError(0, f"нет связи с {self.api.base}: {e}") from None
        except asyncio.TimeoutError:
            raise InvenTreeError(0, f"{self.api.base} не ответил вовремя") from None

    async def get(self, path, **params):
        return await self.call("GET", path, params=params or None)

    async def post(self, path, payload, files=None):
        return await self.call("POST", path, payload, files=files)

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
        """Личный API-токен (клиент должен быть создан через Api.basic)."""
        return (await self.get("/api/user/token/", name=name))["token"]

    async def roles(self):
        return await self.get("/api/user/roles/")

    # --- детали

    async def part(self, pk):
        return await self.get(f"/api/part/{pk}/")

    async def get_or_create_manufacturer(self, name):
        for c in await self.get_all("/api/company/", search=name):
            if c["name"].casefold() == name.casefold():
                return c["pk"]
        res = await self.post("/api/company/", {
            "name": name, "is_manufacturer": True, "is_supplier": False, "is_customer": False,
        })
        return res["pk"]

    async def create_part(self, name, category, manufacturer="", model=""):
        part = await self.post("/api/part/", {
            "name": name, "description": name, "category": category,
            "trackable": True, "purchaseable": True, "active": True,
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
        """Позиции без серийника ровно в этом месте (например, из райдера)."""
        items = await self.get_all("/api/stock/", part=part_pk, location=location_pk, cascade="false")
        return [i for i in items if not i.get("serial") and float(i.get("quantity", 0)) >= 1]

    async def find_by_serial(self, serial):
        items = await self.get_all("/api/stock/", serial=serial, **STOCK_DETAIL)
        return items[0] if items else None

    async def find_by_batch(self, batch, part_pk=None):
        params = {"batch": batch, **STOCK_DETAIL}
        if part_pk:
            params["part"] = part_pk
        return await self.get_all("/api/stock/", **params)

    async def search_stock(self, text, detail=True):
        return await self.get_all("/api/stock/", search=text, **(STOCK_DETAIL if detail else {}))

    async def stock_item(self, pk):
        return await self.get(f"/api/stock/{pk}/", **STOCK_DETAIL)

    async def create_stock(self, part_pk, location_pk, notes):
        res = await self.post("/api/stock/", {
            "part": part_pk, "location": location_pk, "quantity": 1, "status": STATUS_OK, "notes": notes,
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
        """Списывает 1 шт. с позиции (например, из райдера после сверки)."""
        await self.post("/api/stock/remove/", {"items": [{"pk": stock_pk, "quantity": 1}], "notes": note})

    async def attach(self, model_type, model_id, filename, data, comment=""):
        await self.post("/api/attachment/", {"model_type": model_type, "model_id": model_id, "comment": comment},
                        files={"attachment": (filename, data)})


def tree_paths(nodes):
    """{pk: "Звук / Микрофоны / Проводные"} для категорий или мест."""
    by_pk = {n["pk"]: n for n in nodes}
    paths = {}

    def path(pk):
        if pk not in paths:
            parent = by_pk[pk].get("parent")
            name = by_pk[pk]["name"]
            paths[pk] = f"{path(parent)} / {name}" if parent in by_pk else name
        return paths[pk]

    for pk in by_pk:
        path(pk)
    return paths


class Catalog:
    """Кэш справочников (категории, места, детали, модели производителей).

    Загружается параллельно одним заходом и живёт ttl секунд; после создания
    детали сбрасывается. Так распознавание не тянет весь каталог заново.
    """

    def __init__(self, ttl=300):
        self.ttl = ttl
        self._lock = asyncio.Lock()
        self._loaded_at = 0.0
        self.categories = self.locations = self.parts = self.mpns = []
        self.category_paths = self.location_paths = {}

    def invalidate(self):
        self._loaded_at = 0.0

    async def load(self, it):
        async with self._lock:
            if time.monotonic() - self._loaded_at < self.ttl:
                return self
            started = time.monotonic()
            self.categories, self.locations, self.parts, self.mpns = await asyncio.gather(
                it.get_all("/api/part/category/"),
                it.get_all("/api/stock/location/"),
                it.get_all("/api/part/"),
                it.get_all("/api/company/part/manufacturer/"),
            )
            self.category_paths = tree_paths(self.categories)
            self.location_paths = tree_paths(self.locations)
            self._loaded_at = time.monotonic()
            log.info("каталог загружен за %.0f мс: %d категорий, %d мест, %d деталей",
                     (time.monotonic() - started) * 1000, len(self.categories), len(self.locations), len(self.parts))
            return self

    @property
    def model_names(self):
        return sorted(p["name"] for p in self.parts)
