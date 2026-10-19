"""Сайты производителей и магазинов из data/sources.yaml."""

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import yaml

from bot.matching import norm

DEFAULT_FILE = Path(__file__).resolve().parent.parent / "data" / "sources.yaml"


def host(url):
    h = (urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _on(h, domain):
    return h == domain or h.endswith("." + domain)


@dataclass
class Sources:
    manufacturers: dict = field(default_factory=dict)  # norm(бренд) → [домены]
    retailers: list = field(default_factory=list)
    skip: list = field(default_factory=list)

    @classmethod
    def load(cls, path=DEFAULT_FILE):
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls({norm(k): v for k, v in (data.get("manufacturers") or {}).items()},
                   data.get("retailers") or [], data.get("skip") or [])

    def domains(self, manufacturer):
        return self.manufacturers.get(norm(manufacturer), [])

    def is_manufacturer(self, url, manufacturer):
        h = host(url)
        brand = norm(manufacturer)
        return any(_on(h, d) for d in self.domains(manufacturer)) or bool(brand and brand in norm(h))

    def is_retailer(self, url):
        return any(_on(host(url), d) for d in self.retailers)

    def is_skipped(self, url):
        return any(_on(host(url), d) for d in self.skip)
