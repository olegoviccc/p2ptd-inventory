"""Scraper: поиск мануалов и фото против поддельных InvenTree, поиска и сайта производителя."""

import asyncio
import io
from types import SimpleNamespace

from aiohttp import web
from PIL import Image

from bot.inventree import Api
from bot.storage import Storage
from scraper import finders
from scraper.run import format_report, run
from scraper.sources import Sources
from scraper.state import State
from scraper.web import Web, page_image
from tests.fake_inventree import TOKEN


def make_pdf(text, pad=40_000):
    """Минимальный PDF с одной строкой текста (pypdf её извлекает) и добивкой до нужного размера."""
    content = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> "
        b"/Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n%" + b"x" * pad + b"\n")
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def make_png(w=800, h=500, color="red"):
    out = io.BytesIO()
    Image.new("RGB", (w, h), color).save(out, "PNG")
    return out.getvalue()


# --- проверки


def test_pdf_and_image_checks():
    assert finders.pdf_names_model(make_pdf("MG12XU Owner's Manual"), "MG12XU")
    assert not finders.pdf_names_model(make_pdf("MG10XU Owner's Manual"), "MG12XU")
    assert not finders.pdf_names_model(make_pdf("MG12XU", pad=10), "MG12XU")  # слишком маленький
    assert not finders.pdf_names_model(b"<html>not a pdf</html>" * 2000, "MG12XU")
    jpeg = finders.normalize_image(make_png(3000, 2000))
    assert jpeg[:2] == b"\xff\xd8" and max(Image.open(io.BytesIO(jpeg)).size) == 1600
    assert finders.normalize_image(make_png(120, 80)) is None and finders.normalize_image(b"nope") is None


def test_page_image():
    html = '<head><meta property="og:image" content="/img/mg12xu.png"><meta name="twitter:image" content="x"></head>'
    assert page_image(html, "https://usa.yamaha.com/products/mg12xu.html") == "https://usa.yamaha.com/img/mg12xu.png"
    assert page_image("<html></html>", "https://x") is None


def test_sources():
    s = Sources.load()
    assert s.is_manufacturer("https://www.pioneerdj.com/en/support/", "Pioneer DJ")
    assert s.is_manufacturer("https://mediadl.musictribe.com/doc/x.pdf", "Behringer")
    assert s.is_manufacturer("https://www.adj.eu/fog.pdf", "ADJ")
    assert not s.is_manufacturer("https://www.thomann.de/x", "Yamaha") and s.is_retailer("https://www.thomann.de/x")
    assert s.is_skipped("https://www.manualslib.com/manual/1.html")


def test_olegovic_folder(tmp_path):
    (tmp_path / "Pioneer DJ").mkdir()
    (tmp_path / "Pioneer DJ" / "XDJ-1000MK2.pdf").write_bytes(b"%PDF manual")
    assert finders.olegovic_manual(tmp_path, "Pioneer DJ", "XDJ1000MK2").name == "XDJ-1000MK2.pdf"
    assert finders.olegovic_manual(tmp_path, "Yamaha", "XDJ-1000MK2") is None
    assert finders.olegovic_manual(tmp_path / "missing", "Pioneer DJ", "XDJ-1000MK2") is None


# --- прогон целиком


class FakeSearch:
    name = "fake"

    def __init__(self, base):
        self.base, self.queries = base, []

    async def text(self, query, n=10):
        self.queries.append(query)
        if "MG12XU" not in query:
            return []
        return [{"url": f"{self.base}/manuals/MG10XU_om.pdf", "title": "MG10XU manual"},  # другая модель
                {"url": f"{self.base}/manuals/MG12XU_om.pdf", "title": "MG12XU Owner's Manual"},
                {"url": f"{self.base}/products/mg12xu.html", "title": "MG12XU | Yamaha"}]

    async def images(self, query, n=20):
        self.queries.append("img:" + query)
        return []


def respond(body, content_type):
    async def handler(request):
        return web.Response(body=body, content_type=content_type)
    return handler


def site():
    page = '<html><head><meta property="og:image" content="/img/mg12xu.png"></head></html>'
    app = web.Application()
    app.router.add_get("/products/mg12xu.html", respond(page.encode(), "text/html"))
    app.router.add_get("/img/mg12xu.png", respond(make_png(), "image/png"))
    app.router.add_get("/manuals/MG12XU_om.pdf", respond(make_pdf("MG12XU Owner's Manual"), "application/pdf"))
    app.router.add_get("/manuals/MG10XU_om.pdf", respond(make_pdf("MG10XU Owner's Manual"), "application/pdf"))
    return app


def scraper_cfg(tmp_path, **kw):
    folder = tmp_path / "manuals"
    (folder / "Pioneer DJ").mkdir(parents=True)
    (folder / "Pioneer DJ" / "XDJ-1000MK2.pdf").write_bytes(make_pdf("XDJ-1000MK2 Operating Instructions"))
    return SimpleNamespace(manuals=True, photos=True, manual_search=True, manual_mode="file",
                           olegovic_manuals=folder, limit=20, retry_days=30, **kw)


async def with_site(fn):
    runner = web.AppRunner(site())
    await runner.setup()
    tcp = web.TCPSite(runner, "127.0.0.1", 0)
    await tcp.start()
    base = f"http://127.0.0.1:{tcp._server.sockets[0].getsockname()[1]}"
    w = Web(timeout=10)
    try:
        return await fn(base, w)
    finally:
        await w.close()
        await runner.cleanup()


def test_full_run(catalog, tmp_path):
    cfg = scraper_cfg(tmp_path)
    sources = Sources({"yamaha": ["127.0.0.1"]}, [], [])
    state = State(tmp_path / "scraper.sqlite3")
    store = Storage(tmp_path / "bot.sqlite3")

    async def go(base, w):
        api = Api(catalog.url)
        it = api.client(TOKEN)
        search = FakeSearch(base)
        dry = await run(cfg, it, search, w, sources, state, store, dry_run=True)
        attachments_after_dry = len(catalog.db["attachment"])
        first = await run(cfg, it, search, w, sources, state, store)
        second = await run(cfg, it, search, w, sources, state, store)
        await api.close()
        return dry, attachments_after_dry, first, second, search

    dry, after_dry, first, second, search = asyncio.run(with_site(go))

    assert after_dry == 0 and dry.manuals and dry.photos  # проверка ничего не пишет
    assert (first.parts, first.no_model, first.missing_manual, first.missing_photo) == (3, 1, 2, 2)
    assert sorted(first.manuals) == [("Pioneer DJ XDJ-1000MK2", "olegovic"), ("Yamaha MG12XU", "manufacturer")]
    assert first.photos == [("Yamaha MG12XU", "manufacturer")]
    assert first.not_found == [("Pioneer DJ XDJ-1000MK2", "фото")]

    manuals = {int(a["model_id"]): a for a in catalog.db["attachment"]}
    assert set(manuals) == {1, 2} and b"MG12XU" in manuals[2]["attachment"]  # подошёл PDF именно этой модели
    assert "Руководство" in manuals[2]["comment"] and "MG12XU_om.pdf" in manuals[2]["comment"]
    assert catalog.part_images[2][:2] == b"\xff\xd8"
    assert not any("XDJ" in q and "manual" in q for q in search.queries)  # мануал XDJ взят из папки, без поиска

    assert second.checked == 0  # всё найденное приложено, ненайденное фото ждёт 30 дней
    assert {e.action for e in store.recent_events()} == {"manual_added", "image_added"}
    assert "📘 Мануалы" in format_report(first) and "Не нашлось" in format_report(first)


def test_link_mode_and_retry(catalog, tmp_path):
    cfg = scraper_cfg(tmp_path)
    cfg.manual_mode, cfg.photos = "link", False
    cfg.olegovic_manuals = None
    state = State(tmp_path / "scraper.sqlite3")

    async def go(base, w):
        api = Api(catalog.url)
        it = api.client(TOKEN)
        report = await run(cfg, it, FakeSearch(base), w, Sources({"yamaha": ["127.0.0.1"]}, [], []), state)
        forced = await run(cfg, it, FakeSearch(base), w, Sources(), state, part_pk=1, force=True)
        await api.close()
        return base, report, forced

    base, report, forced = asyncio.run(with_site(go))
    link = next(a for a in catalog.db["attachment"] if int(a["model_id"]) == 2)
    assert link["link"] == f"{base}/manuals/MG12XU_om.pdf"
    assert report.not_found == [("Pioneer DJ XDJ-1000MK2", "мануал")] and not state.due(1, "manual", 30)
    assert forced.checked == 1  # --part ищет снова, не дожидаясь 30 дней


def test_ddg_image_sizes_are_strings():
    """ddgs отдаёт ширину и высоту строками — сортировка кандидатов падала с TypeError."""
    from scraper.search import DDGSearch

    async def fake_run(method, query, n):
        return [{"image": "https://a/1.jpg", "url": "https://a", "title": "MG12XU", "width": "800", "height": "600"},
                {"image": "https://a/2.jpg", "url": "https://a", "title": "MG12XU", "width": "", "height": None}]
    s = DDGSearch()
    s._run = fake_run
    images = asyncio.run(s.images("Yamaha MG12XU"))
    assert [(i["width"], i["height"]) for i in images] == [(800, 600), (0, 0)]


class BrokenSearch(FakeSearch):
    async def text(self, query, n=10):
        raise TypeError("bad operand type for unary -: 'str'")


def test_part_error_goes_to_journal(catalog, tmp_path):
    cfg = scraper_cfg(tmp_path)
    cfg.olegovic_manuals = None
    store = Storage(tmp_path / "bot.sqlite3")

    async def go(base, w):
        api = Api(catalog.url)
        report = await run(cfg, api.client(TOKEN), BrokenSearch(base), w, Sources(), State(tmp_path / "s.sqlite3"), store)
        await api.close()
        return report

    report = asyncio.run(with_site(go))
    assert report.errors and "TypeError" in report.errors[0]
    errors = [e for e in store.recent_events() if e.action == "error"]
    assert errors and errors[0].username == "scraper" and "unary" in errors[0].details["error"]
