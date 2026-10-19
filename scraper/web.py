"""Загрузка страниц, PDF и картинок с ограничением размера."""

import re
from urllib.parse import urljoin

import aiohttp

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) p2ptd-inventory-scraper (+internal equipment catalogue)"


class FetchError(Exception):
    pass


class Web:
    def __init__(self, timeout=30, max_bytes=40 * 1024 * 1024):
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.max_bytes = max_bytes
        self._session = None

    def session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout, headers={"User-Agent": USER_AGENT})
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def get(self, url, max_bytes=None):
        """(байты, content-type); не больше max_bytes."""
        limit = max_bytes or self.max_bytes
        try:
            async with self.session().get(url, allow_redirects=True) as resp:
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
