"""«🐞 Диагностика»: файл для разработчика без секретов, логи каталога в отдельном файле."""

import asyncio
import logging

from bot import diagnostics, logs
from tests.fake_inventree import TOKEN
from tests.test_flow import message, press
from tests.test_scraper import with_site
from tests.test_scraper_control import ScriptedVerifier, bot_dispatcher, make_scraper

BOT_TOKEN = "1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw1"


def test_mask():
    text = (f"bot{BOT_TOKEN}/sendMessage Authorization: Token inv-0a1b2c3d4e5f6a7b8c9d-20261019 "
            "ANTHROPIC_API_KEY=sk-ant-api03-abcdefghijklmnop url?token=s3cr3tvalue123 мой-пароль-123")
    masked = diagnostics.mask(text, known=["мой-пароль-123"])
    for secret in (BOT_TOKEN, "inv-0a1b", "sk-ant", "s3cr3tvalue123", "мой-пароль-123"):
        assert secret not in masked
    assert masked.count(diagnostics.HIDDEN) == 5 and "/sendMessage" in masked


def test_tail_reads_rotated_file(tmp_path):
    log = tmp_path / "bot.log"
    log.with_name("bot.log.1").write_text("".join(f"old {i}\n" for i in range(10)))
    log.write_text("new 0\nnew 1\n")
    assert diagnostics.tail(log, 4) == ["old 8", "old 9", "new 0", "new 1"]
    assert diagnostics.tail(tmp_path / "missing.log") == []


def test_scraper_logger_gets_own_file(tmp_path):
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        logs.setup(tmp_path, extra={"scraper": "scraper.log"})
        logging.getLogger("scraper.finders").info("модель отклонила x.pdf")
        logging.getLogger("bot.handlers").info("только в логе бота")
        for h in root.handlers + logging.getLogger("scraper").handlers:
            h.flush()
    finally:
        for h in root.handlers + logging.getLogger("scraper").handlers:
            h.close()
        logging.getLogger("scraper").handlers.clear()
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
    scraper_log = (tmp_path / "logs" / "scraper.log").read_text()
    assert "модель отклонила" in scraper_log and "только в логе бота" not in scraper_log
    assert "модель отклонила" in (tmp_path / "logs" / "bot.log").read_text()


async def diag_scenario(catalog, tmp_path, base, w):
    s, service_api = make_scraper(catalog, tmp_path, base, w, ScriptedVerifier())
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "bot.log").write_text(
        f"ERROR необработанная ошибка token={TOKEN}\nTraceback (most recent call last):\nTypeError: boom\n")
    tg, dp, bot, api = bot_dispatcher(catalog, tmp_path, s)
    send = lambda update: dp.feed_update(bot, update)  # noqa: E731
    try:
        await send(message("🐞 Диагностика"))
        name, data = tg.documents[-1]
        text = data.decode()
        assert name.startswith("p2ptd-diag-") and name.endswith(".txt")
        assert "== Каталог" in text and "TypeError: boom" in text and "InvenTree 1.1.0" in text
        assert TOKEN not in text and diagnostics.HIDDEN in text

        await send(press(tg.last_buttons()["📄 bot.log"]))
        name, data = tg.documents[-1]
        assert name == "bot.txt" and b"Traceback" in data and TOKEN.encode() not in data
        await send(press(tg.last_buttons()["📄 scraper.log"]))  # файла нет — только всплывающее сообщение
        assert len(tg.documents) == 2
    finally:
        await api.close()
        await service_api.close()


def test_diagnostics_in_bot(catalog, tmp_path):
    asyncio.run(with_site(lambda base, w: diag_scenario(catalog, tmp_path, base, w)))
