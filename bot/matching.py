"""Сопоставление распознанной модели с деталями InvenTree."""

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

FUZZY_THRESHOLD = 88


def norm(text):
    """Pioneer DJ XDJ-1000 MK2 → pioneerdjxdj1000mk2"""
    return re.sub(r"[^0-9a-zа-яё]", "", (text or "").casefold())


@dataclass
class Match:
    part: dict
    score: int  # 100 = точное совпадение модели


def best_match(manufacturer, model, parts, mpns):
    """Ищет деталь сначала по модели производителя (MPN), потом по названию.

    parts: список деталей /api/part/, mpns: список /api/company/part/manufacturer/.
    """
    if not model:
        return None
    by_pk = {p["pk"]: p for p in parts}
    key = norm(model)

    for m in mpns:
        if norm(m["MPN"]) == key and m["part"] in by_pk:
            return Match(by_pk[m["part"]], 100)

    full = norm(f"{manufacturer} {model}")
    for p in parts:
        if norm(p["name"]) in (full, key):
            return Match(p, 100)

    best = None
    for p in parts:
        score = max(fuzz.ratio(norm(p["name"]), full), fuzz.ratio(norm(p["name"]), key))
        if score >= FUZZY_THRESHOLD and (best is None or score > best.score):
            best = Match(p, int(score))
    return best
