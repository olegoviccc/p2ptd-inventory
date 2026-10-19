"""Поисковики scraper: Tavily API, цепочка с запасным поиском, выбор в настройках."""

import asyncio

import pytest
from aiohttp import web

from scraper import config
from scraper.search import ChainSearch, SearchError, TavilySearch
from scraper.web import Web


async def with_tavily(status, fn):
    """Поддельный api.tavily.com/search; запоминает запросы."""
    seen = []

    async def search(request):
        body = await request.json()
        seen.append((request.headers.get("Authorization"), body))
        if status != 200:
            return web.json_response({"detail": {"error": "usage limit exceeded"}}, status=status)
        return web.json_response({
            "query": body["query"],
            "results": [{"title": "MG12XU Owner's Manual", "url": "https://usa.yamaha.com/mg12xu_om.pdf",
                         "content": "…", "score": 0.9}],
            "images": [{"url": "https://usa.yamaha.com/img/mg12xu.png", "description": "Yamaha MG12XU mixer"},
                       "https://cdn.x/plain.jpg"] if body.get("include_images") else []})
    app = web.Application()
    app.router.add_post("/search", search)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    w = Web(timeout=5)
    tavily = TavilySearch("tvly-test", w.session)
    tavily.URL = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/search"
    try:
        return await fn(tavily), seen
    finally:
        await w.close()
        await runner.cleanup()


def test_tavily_text_and_images():
    async def go(t):
        return await t.text("MG12XU manual site:yamaha.com site:usa.yamaha.com", 10), await t.images("Yamaha MG12XU", 20)

    (text, images), seen = asyncio.run(with_tavily(200, go))
    assert text == [{"url": "https://usa.yamaha.com/mg12xu_om.pdf", "title": "MG12XU Owner's Manual"}]
    assert images[0] == {"image": "https://usa.yamaha.com/img/mg12xu.png", "url": "", "title": "Yamaha MG12XU mixer",
                         "width": 0, "height": 0}
    assert images[1]["image"] == "https://cdn.x/plain.jpg"
    auth, body = seen[0]
    assert auth == "Bearer tvly-test" and body["query"] == "MG12XU manual"
    assert body["include_domains"] == ["yamaha.com", "usa.yamaha.com"] and body["search_depth"] == "basic"
    assert seen[1][1]["include_images"] is True and "include_domains" not in seen[1][1]


class Backup:
    name = "ddgs"

    async def text(self, query, n=10):
        return [{"url": "https://backup", "title": query}]

    async def images(self, query, n=20):
        raise SearchError("ddgs: No results found.")


def test_chain_falls_back_when_credits_run_out():
    async def go(t):
        chain = ChainSearch([t, Backup()])
        text = await chain.text("MG12XU", 10)
        with pytest.raises(SearchError, match="tavily: 432.*ddgs: No results"):
            await chain.images("MG12XU", 20)
        return chain.name, text

    (name, text), seen = asyncio.run(with_tavily(432, go))
    assert name == "tavily → ddgs" and text == [{"url": "https://backup", "title": "MG12XU"}] and len(seen) == 2


def test_search_settings(monkeypatch):
    monkeypatch.setattr(config, "load_env", lambda: None)
    for k in ("SCRAPER_SEARCH", "BRAVE_API_KEY", "TAVILY_API_KEY", "TAVILY_SEARCH_DEPTH"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("INVENTREE_URL", "http://inv")
    monkeypatch.setenv("INVENTREE_TOKEN", "t")
    monkeypatch.setenv("SCRAPER_VERIFY", "off")
    assert config.load().search == ("ddgs",)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    assert config.load().search == ("tavily", "ddgs")
    monkeypatch.setenv("SCRAPER_SEARCH", "brave")
    with pytest.raises(SystemExit, match="BRAVE_API_KEY"):
        config.load()
    monkeypatch.setenv("SCRAPER_SEARCH", "google")
    with pytest.raises(SystemExit, match="SCRAPER_SEARCH"):
        config.load()
