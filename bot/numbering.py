"""Внутренние инвентарные номера вида P2PTD-00042."""

import re


class Numbering:
    def __init__(self, prefix="P2PTD-", digits=5):
        self.prefix = prefix
        self.digits = digits
        self.pattern = re.compile(re.escape(prefix) + r"(\d+)$")

    def format(self, n):
        return f"{self.prefix}{n:0{self.digits}d}"

    def parse(self, serial):
        m = self.pattern.match(serial or "")
        return int(m.group(1)) if m else None

    def is_internal(self, text):
        return self.parse(text.strip().upper()) is not None

    def next_after(self, serials):
        """Следующий номер после максимального из уже выданных."""
        used = [n for n in (self.parse(s) for s in serials) if n is not None]
        return self.format(max(used, default=0) + 1)
