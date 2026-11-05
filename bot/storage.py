"""SQLite: привязки Telegram-пользователей к InvenTree и журнал действий."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class User:
    tg_id: int
    username: str
    token: str
    location_pk: int | None
    location_label: str
    share_labels: bool = False  # каждую новую этикетку сразу отправлять получателю (BOT_LABEL_RECIPIENT)


@dataclass
class Event:
    ts: str
    tg_id: int
    username: str
    action: str
    details: dict


class Storage:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                tg_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL,
                token TEXT NOT NULL,
                location_pk INTEGER,
                location_label TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                tg_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                action TEXT NOT NULL,
                details TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
        """)
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(users)")}
        if "share_labels" not in columns:
            with self.db:
                self.db.execute("ALTER TABLE users ADD COLUMN share_labels INTEGER NOT NULL DEFAULT 0")

    # --- пользователи

    def get(self, tg_id):
        row = self.db.execute(
            "SELECT tg_id, username, token, location_pk, location_label, share_labels FROM users WHERE tg_id = ?",
            (tg_id,)
        ).fetchone()
        return User(*row[:5], share_labels=bool(row[5])) if row else None

    def tg_id_of(self, username):
        """Telegram ID пользователя InvenTree, который входил в бота; None, если не входил."""
        row = self.db.execute("SELECT tg_id FROM users WHERE lower(username) = lower(?)", (username,)).fetchone()
        return row[0] if row else None

    def set_share_labels(self, tg_id, on):
        with self.db:
            self.db.execute("UPDATE users SET share_labels = ? WHERE tg_id = ?", (int(on), tg_id))

    def save(self, tg_id, username, token):
        with self.db:
            self.db.execute(
                "INSERT INTO users (tg_id, username, token) VALUES (?, ?, ?) "
                "ON CONFLICT(tg_id) DO UPDATE SET username = excluded.username, token = excluded.token",
                (tg_id, username, token),
            )

    def set_location(self, tg_id, location_pk, label):
        with self.db:
            self.db.execute("UPDATE users SET location_pk = ?, location_label = ? WHERE tg_id = ?",
                            (location_pk, label, tg_id))

    def delete(self, tg_id):
        with self.db:
            self.db.execute("DELETE FROM users WHERE tg_id = ?", (tg_id,))

    # --- журнал действий

    def log_event(self, tg_id, username, action, **details):
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.db:
            self.db.execute(
                "INSERT INTO events (ts, tg_id, username, action, details) VALUES (?, ?, ?, ?, ?)",
                (ts, tg_id, username or "", action, json.dumps(details, ensure_ascii=False)),
            )

    def recent_events(self, limit=20):
        rows = self.db.execute(
            "SELECT ts, tg_id, username, action, details FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [Event(ts, tg_id, username, action, json.loads(details)) for ts, tg_id, username, action, details in rows]
