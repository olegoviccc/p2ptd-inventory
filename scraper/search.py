"""Веб-поиск: DuckDuckGo без ключа (как в olegovic-llm), Brave Search API или Tavily API; можно цепочкой."""

import asyncio
import logging
import re

import aiohttp

log = logging.getLogger(__name__)


class SearchError(Exception):
    pass


def _int(value):
    """Размер картинки из поиска: ddgs отдаёт строки («800»), иногда пустые или с мусором."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


class DDGSearch:
    """Пакет ddgs: без ключа, но поисковик может ограничивать частые запросы."""

    name = "ddgs"

    def __init__(self, pause=2.0):
        self.pause = pause  # пауза между запросами, чтобы не получить блокировку

    async def _run(self, method, query, n):
        from ddgs import DDGS
        try:
            results = await asyncio.to_thread(lambda: getattr(DDGS(), method)(query, max_results=n))
        except Exception as e:  # noqa: BLE001 — ddgs бросает разные исключения на сетевые сбои и лимиты
            raise SearchError(f"ddgs: {e}") from e
        finally:
            await asyncio.sleep(self.pause)
        return results or []

    async def text(self, query, n=10):
        return [{"url": r.get("href", ""), "title": r.get("title", "")} for r in await self._run("text", query, n)]

    async def images(self, query, n=20):
        return [{"image": r.get("image", ""), "url": r.get("url", ""), "title": r.get("title", ""),
                 "width": _int(r.get("width")), "height": _int(r.get("height"))}
                for r in await self._run("images", query, n)]


class BraveSearch:
    """Brave Search API: стабильнее, бесплатный тариф ~2000 запросов в месяц."""

    name = "brave"
    BASE = "https://api.search.brave.com/res/v1"

    def __init__(self, key, session_factory):
        self.key = key
        self.session = session_factory

    async def _get(self, path, query, n):
        try:
            async with self.session().get(f"{self.BASE}/{path}", params={"q": query, "count": n},
                                          headers={"X-Subscription-Token": self.key,
                                                   "Accept": "application/json"}) as resp:
                if resp.status >= 400:
                    raise SearchError(f"brave: {resp.status} {(await resp.text())[:200]}")
                return await resp.json()
        except aiohttp.ClientError as e:
            raise SearchError(f"brave: {e}") from e

    async def text(self, query, n=10):
        data = await self._get("web/search", query, n)
        return [{"url": r.get("url", ""), "title": r.get("title", "")} for r in data.get("web", {}).get("results", [])]

    async def images(self, query, n=20):
        data = await self._get("images/search", query, n)
        return [{"image": (r.get("properties") or {}).get("url", ""), "url": r.get("url", ""),
                 "title": r.get("title", ""), "width": 0, "height": 0} for r in data.get("results", [])]


SITE = re.compile(r"\s*\bsite:(\S+)")


class TavilySearch:
    """Tavily API: поиск для LLM-агентов, бесплатно 1000 кредитов в месяц (basic — 1 кредит за запрос).
    «site:домен» из запроса передаётся фильтром include_domains."""

    name = "tavily"
    URL = "https://api.tavily.com/search"

    def __init__(self, key, session_factory, depth="basic"):
        self.key, self.session, self.depth = key, session_factory, depth

    async def _search(self, query, n, **extra):
        domains = SITE.findall(query)
        payload = {"query": SITE.sub("", query).strip(), "max_results": max(1, min(n, 20)),
                   "search_depth": self.depth, **extra}
        if domains:
            payload["include_domains"] = domains
        try:
            async with self.session().post(self.URL, json=payload,
                                           headers={"Authorization": f"Bearer {self.key}"}) as resp:
                if resp.status >= 400:
                    raise SearchError(f"tavily: {resp.status} {(await resp.text())[:200]}")
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as e:
            raise SearchError(f"tavily: {e}") from e

    async def text(self, query, n=10):
        data = await self._search(query, n)
        return [{"url": r.get("url", ""), "title": r.get("title", "")} for r in data.get("results") or []]

    async def images(self, query, n=20):
        # Tavily отдаёт картинки без страницы-источника и без размеров; описание — как подпись
        data = await self._search(query, 5, include_images=True, include_image_descriptions=True)
        images = []
        for im in data.get("images") or []:
            url, title = (im.get("url", ""), im.get("description", "")) if isinstance(im, dict) else (str(im), "")
            images.append({"image": url, "url": "", "title": title or "", "width": 0, "height": 0})
        return images[:n]


class ChainSearch:
    """Поисковики по очереди: следующий — если предыдущий ответил ошибкой (лимит, кредиты кончились, сеть)."""

    def __init__(self, engines):
        self.engines = engines
        self.name = " → ".join(e.name for e in engines)

    async def _first(self, method, query, n):
        errors = []
        for engine in self.engines:
            try:
                return await getattr(engine, method)(query, n)
            except SearchError as e:
                if engine is not self.engines[-1]:
                    log.info("поиск %s не удался (%s), пробую следующий", engine.name, e)
                errors.append(str(e))
        raise SearchError("; ".join(errors))

    async def text(self, query, n=10):
        return await self._first("text", query, n)

    async def images(self, query, n=20):
        return await self._first("images", query, n)
