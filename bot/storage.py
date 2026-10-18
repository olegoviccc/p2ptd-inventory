"""Привязки Telegram-пользователей к аккаунтам InvenTree (SQLite)."""

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass
class User:
    tg_id: int
    username: str
    token: str
    location_pk: int | None
    location_label: str


class Storage:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                tg_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL,
                token TEXT NOT NULL,
                location_pk INTEGER,
                location_label TEXT NOT NULL DEFAULT ''
            )""")
        self.db.commit()

    def get(self, tg_id):
        row = self.db.execute(
            "SELECT tg_id, username, token, location_pk, location_label FROM users WHERE tg_id = ?", (tg_id,)
        ).fetchone()
        return User(*row) if row else None

    def save(self, tg_id, username, token):
        self.db.execute(
            "INSERT INTO users (tg_id, username, token) VALUES (?, ?, ?) "
            "ON CONFLICT(tg_id) DO UPDATE SET username = excluded.username, token = excluded.token",
            (tg_id, username, token),
        )
        self.db.commit()

    def set_location(self, tg_id, location_pk, label):
        self.db.execute("UPDATE users SET location_pk = ?, location_label = ? WHERE tg_id = ?",
                        (location_pk, label, tg_id))
        self.db.commit()

    def delete(self, tg_id):
        self.db.execute("DELETE FROM users WHERE tg_id = ?", (tg_id,))
        self.db.commit()
