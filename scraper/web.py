"""Загрузка страниц, PDF и картинок с ограничением размера."""

import asyncio
import html as htmllib
import logging
import re
from urllib.parse import urljoin, urlparse

import aiohttp

log = logging.getLogger(__name__)

# Сайты за Cloudflare (adj.com и др.) отвечают 429/403 на «ботовый» User-Agent — представляемся браузером
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
}
RETRY_STATUSES = (429, 503)


class FetchError(Exception):
    pass


class Web:
    def __init__(self, timeout=30, max_bytes=40 * 1024 * 1024, retry_wait=5):
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.max_bytes = max_bytes
        self.retry_wait = retry_wait  # пауза перед повтором после 429/503, если сайт не назвал свою
        self._session = None

    def session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout, headers=HEADERS)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def get(self, url, max_bytes=None):
        """(байты, content-type); не больше max_bytes. После 429/503 — один повтор с паузой."""
        try:
            return await self._get(url, max_bytes)
        except _Retry as e:
            log.info("%s: HTTP %s, повтор через %.0f с", url, e.status, e.wait)
            await asyncio.sleep(e.wait)
        try:
            return await self._get(url, max_bytes)
        except _Retry as e:
            raise FetchError(f"{url}: HTTP {e.status}") from None

    async def _get(self, url, max_bytes):
        limit = max_bytes or self.max_bytes
        try:
            async with self.session().get(url, allow_redirects=True) as resp:
                if resp.status in RETRY_STATUSES:
                    raise _Retry(resp.status, _retry_after(resp.headers.get("Retry-After"), self.retry_wait))
                if resp.status >= 400:
                    raise FetchError(f"{url}: HTTP {resp.status}")
                if int(resp.headers.get("Content-Length") or 0) > limit:
                    raise FetchError(f"{url}: больше {limit // 1024 // 1024} МБ")
                data = bytearray()
                async for chunk in resp.content.iter_chunked(65536):
                    data += chunk
                    if len(data) > limit:
                        raise FetchError(f"{url}: больше {limit // 1024 // 1024} МБ")
                return bytes(data), resp.headers.get("Content-Type", "")
        except (aiohttp.ClientError, TimeoutError) as e:
            raise FetchError(f"{url}: {e}") from e


class _Retry(Exception):
    def __init__(self, status, wait):
        self.status, self.wait = status, wait


def _retry_after(value, default):
    try:
        return min(max(float(value), 1), 20)
    except (TypeError, ValueError):
        return default


def is_html(data, content_type=""):
    return "html" in (content_type or "").lower() or data.lstrip()[:15].lower().startswith((b"<!doctype", b"<html"))


LINK = re.compile(r"""<a\b[^>]*?href\s*=\s*["']([^"'#]+)["'][^>]*>(.*?)</a>""", re.I | re.S)
TAGS = re.compile(r"<[^>]+>")


def pdf_links(html, base_url):
    """Ссылки на PDF со страницы: [(url, текст ссылки)] без повторов."""
    found = {}
    for href, text in LINK.findall(html):
        url = urljoin(base_url, htmllib.unescape(href.strip()))
        path = urlparse(url).path.lower()
        if urlparse(url).scheme in ("http", "https") and (path.endswith(".pdf") or "/pdf/" in path):
            found.setdefault(url, " ".join(htmllib.unescape(TAGS.sub(" ", text)).split()))
    return list(found.items())


META_IMAGE = re.compile(
    r"""<meta[^>]+(?:property|name)\s*=\s*["'](?:og:image(?::secure_url)?|twitter:image)["'][^>]*>""", re.I)
CONTENT = re.compile(r"""content\s*=\s*["']([^"']+)["']""", re.I)


def page_image(html, base_url):
    """Картинка товара со страницы: og:image / twitter:image."""
    for tag in META_IMAGE.findall(html):
        m = CONTENT.search(tag)
        if m:
            return urljoin(base_url, m.group(1).strip())
    return None
