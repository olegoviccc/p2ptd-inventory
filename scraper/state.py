"""Что и когда уже искали: не найденное повторяем только через retry_days."""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


class State:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS tries (
                part_pk INTEGER NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                tried_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (part_pk, kind)
            )""")
        self.db.commit()

    def due(self, part_pk, kind, retry_days):
        row = self.db.execute("SELECT status, tried_at FROM tries WHERE part_pk = ? AND kind = ?",
                              (part_pk, kind)).fetchone()
        if not row or row[0] == "found":  # найденное, но потом удалённое из InvenTree — ищем снова
            return True
        return datetime.fromisoformat(row[1]) < datetime.now(timezone.utc) - timedelta(days=retry_days)

    def record(self, part_pk, kind, status, source="", url=""):
        with self.db:
            self.db.execute(
                "INSERT INTO tries (part_pk, kind, status, tried_at, source, url) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(part_pk, kind) DO UPDATE SET status = excluded.status, tried_at = excluded.tried_at, "
                "source = excluded.source, url = excluded.url",
                (part_pk, kind, status, datetime.now(timezone.utc).isoformat(timespec="seconds"), source, url or ""))
