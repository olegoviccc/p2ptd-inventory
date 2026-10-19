"""Внутренние инвентарные номера вида P2PTD0042.

Старый формат с дефисом (P2PTD-00042) тоже распознаётся: такие номера
учитываются при выдаче следующего и находятся поиском.
"""

import re


class Numbering:
    def __init__(self, prefix="P2PTD", digits=4):
        self.prefix = prefix
        self.digits = digits
        self.pattern = re.compile(re.escape(prefix.rstrip("-")) + r"-?(\d+)$", re.IGNORECASE)
        self.last = None  # последний известный номер; None = спросить у InvenTree

    def format(self, n):
        return f"{self.prefix}{n:0{self.digits}d}"

    def parse(self, serial):
        m = self.pattern.match((serial or "").strip())
        return int(m.group(1)) if m else None

    def is_internal(self, text):
        return self.parse(text) is not None

    def normalize(self, text):
        """Любое написание номера (p2ptd-00042, P2PTD42) → текущий формат."""
        n = self.parse(text)
        return self.format(n) if n is not None else text

    def highest(self, serials):
        return max((n for n in map(self.parse, serials) if n is not None), default=0)
