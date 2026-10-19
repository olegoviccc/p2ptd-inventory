"""Движок scraper для ночного прогона и для бота: настройки, блокировка, очередь на проверку."""

import fcntl
import logging
from pathlib import Path
from types import SimpleNamespace

from bot.llm import LocalLLM

from .finders import Found
from .run import Report, attach_manual, run, targets
from .search import BraveSearch, ChainSearch, DDGSearch, TavilySearch
from .sources import Sources
from .state import State
from .verify import NoVerifier, Verifier
from .web import Web

log = logging.getLogger(__name__)


class Busy(Exception):
    pass


class Scraper:
    def __init__(self, cfg, it, state, sources, web, search, llm=None, store=None):
        self.cfg, self.it, self.state, self.sources = cfg, it, state, sources
        self.web, self.search, self.llm, self.store = web, search, llm, store
        self.progress = None  # Report текущего прогона, пока он идёт

    # --- настройки (из бота, иначе из .env)

    def settings(self):
        return {
            "nightly": self.state.setting("nightly", True),
            "manuals": self.state.setting("manuals", self.cfg.manuals),
            "photos": self.state.setting("photos", self.cfg.photos),
            "verify": self.state.setting("verify", self.cfg.verify) and self.llm is not None,
            "limit": self.state.setting("limit", self.cfg.limit),
        }

    def effective(self):
        s = self.settings()
        return SimpleNamespace(**{**vars(self.cfg), "manuals": s["manuals"], "photos": s["photos"],
                                  "limit": s["limit"]})

    def verifier(self):
        return Verifier(self.llm) if self.settings()["verify"] else NoVerifier()

    # --- прогоны

    @property
    def running(self):
        return self.progress is not None

    async def stats(self):
        report = Report()
        await targets(self.it, report)
        return report

    async def run(self, trigger, *, dry_run=False, limit=None, only=None, part_pk=None, force=False):
        """Прогон; Busy, если уже идёт (в этом процессе или ночной в другом)."""
        if self.running:
            raise Busy("поиск уже идёт")
        lock = open(self.state.dir / "scraper.lock", "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise Busy("сейчас идёт ночной прогон") from None
        self.progress = Report()
        run_id = self.state.start_run(f"{trigger} (проверка)" if dry_run else trigger)
        try:
            return await run(self.effective(), self.it, self.search, self.web, self.sources, self.state,
                             None if dry_run else self.store, only=only, limit=limit, dry_run=dry_run,
                             part_pk=part_pk, force=force, verifier=self.verifier(), report=self.progress)
        finally:
            self.state.finish_run(run_id, self.progress.summary())
            self.progress = None
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()

    # --- очередь «На проверку»

    async def accept(self, review_id, author=""):
        r = self.state.review(review_id)
        if not r:
            return None
        data = r.path.read_bytes()
        if r.kind == "manual":
            name = r.path.name.split("_", 1)[-1]
            await attach_manual(self.it, self.cfg.manual_mode, r.part_pk, Found(data, r.url or None, r.source, name))
        else:
            await self.it.set_part_image(r.part_pk, r.path.name.split("_", 1)[-1], data)
        self.state.close_review(review_id, accepted=True)
        if self.store:
            self.store.log_event(0, author or "scraper", "manual_added" if r.kind == "manual" else "image_added",
                                 model=r.part_name, source="проверено человеком")
        return r

    def reject(self, review_id):
        return self.state.close_review(review_id, accepted=False)

    async def close(self):
        await self.web.close()
        if self.llm:
            await self.llm.close()


def make_search(cfg, web):
    engines = {"ddgs": DDGSearch, "brave": lambda: BraveSearch(cfg.brave_key, web.session),
               "tavily": lambda: TavilySearch(cfg.tavily_key, web.session, cfg.tavily_depth)}
    chain = [engines[name]() for name in cfg.search]
    return chain[0] if len(chain) == 1 else ChainSearch(chain)


def build(cfg, api, store=None):
    web = Web()
    search = make_search(cfg, web)
    llm = LocalLLM(cfg.llm["url"], cfg.llm["model"], timeout=cfg.llm["timeout"], think=cfg.llm["think"]) \
        if cfg.llm else None
    return Scraper(cfg, api.client(cfg.inventree_token), State(Path(cfg.data_dir) / "scraper.sqlite3"),
                   Sources.load(), web, search, llm, store)
