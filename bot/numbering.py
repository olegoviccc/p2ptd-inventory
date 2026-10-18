"""Внутренние инвентарные номера вида P2PTD-00042."""

import re


class Numbering:
    def __init__(self, prefix="P2PTD-", digits=5):
        self.prefix = prefix
        self.digits = digits
        self.pattern = re.compile(re.escape(prefix) + r"(\d+)$", re.IGNORECASE)
        self.last = None  # последний известный номер; None = спросить у InvenTree

    def format(self, n):
        return f"{self.prefix}{n:0{self.digits}d}"

    def parse(self, serial):
        m = self.pattern.match((serial or "").strip())
        return int(m.group(1)) if m else None

    def is_internal(self, text):
        return self.parse(text) is not None

    def highest(self, serials):
        return max((n for n in map(self.parse, serials) if n is not None), default=0)
