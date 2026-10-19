"""Состояние scraper (SQLite, общий для ночного прогона и бота).

* tries — что и когда искали: не найденное повторяем через retry_days;
* review — находки, в которых модель не уверена: ждут решения в боте (файл лежит в review/);
* rejected — что человек отклонил: больше не предлагаем;
* settings — переключатели из бота (перекрывают .env);
* runs — история прогонов для экрана «Каталог».
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

SETTINGS = {  # ключ → (тип, значение по умолчанию берётся из Config)
    "nightly": bool, "manuals": bool, "photos": bool, "verify": bool, "limit": int,
}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Review:
    id: int
    part_pk: int
    part_name: str
    kind: str        # manual | photo
    url: str
    source: str
    reason: str
    path: Path
    created: str


class State:
    def __init__(self, path):
        self.dir = Path(path).parent
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS tries (
                part_pk INTEGER NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, tried_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '', PRIMARY KEY (part_pk, kind));
            CREATE TABLE IF NOT EXISTS review (
                id INTEGER PRIMARY KEY AUTOINCREMENT, part_pk INTEGER NOT NULL, part_name TEXT NOT NULL,
                kind TEXT NOT NULL, url TEXT NOT NULL, source TEXT NOT NULL, reason TEXT NOT NULL,
                path TEXT NOT NULL, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS rejected (
                part_pk INTEGER NOT NULL, kind TEXT NOT NULL, url TEXT NOT NULL, PRIMARY KEY (part_pk, kind, url));
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT NOT NULL, finished TEXT,
                trigger TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '{}');
        """)

    # --- попытки

    def due(self, part_pk, kind, retry_days):
        row = self.db.execute("SELECT status, tried_at FROM tries WHERE part_pk = ? AND kind = ?",
                              (part_pk, kind)).fetchone()
        if not row or row[0] == "found":  # найденное, но потом удалённое из InvenTree — ищем снова
            return True
        if row[0] == "review":  # ждёт решения в боте
            return False
        return datetime.fromisoformat(row[1]) < datetime.now(timezone.utc) - timedelta(days=retry_days)

    def record(self, part_pk, kind, status, source="", url=""):
        with self.db:
            self.db.execute(
                "INSERT INTO tries (part_pk, kind, status, tried_at, source, url) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(part_pk, kind) DO UPDATE SET status = excluded.status, tried_at = excluded.tried_at, "
                "source = excluded.source, url = excluded.url",
                (part_pk, kind, status, now(), source, url or ""))

    # --- очередь «На проверку»

    def add_review(self, part_pk, part_name, kind, found):
        folder = self.dir / "review"
        folder.mkdir(exist_ok=True)
        with self.db:
            cur = self.db.execute(
                "INSERT INTO review (part_pk, part_name, kind, url, source, reason, path, created) "
                "VALUES (?, ?, ?, ?, ?, ?, '', ?)",
                (part_pk, part_name, kind, found.url or "", found.source, found.reason, now()))
            path = folder / f"{cur.lastrowid}_{found.filename}"
            path.write_bytes(found.data)
            self.db.execute("UPDATE review SET path = ? WHERE id = ?", (str(path), cur.lastrowid))
        self.record(part_pk, kind, "review", found.source, found.url)
        return cur.lastrowid

    def reviews(self):
        rows = self.db.execute("SELECT id, part_pk, part_name, kind, url, source, reason, path, created "
                               "FROM review ORDER BY id").fetchall()
        return [Review(*r[:7], Path(r[7]), r[8]) for r in rows]

    def review(self, review_id):
        return next((r for r in self.reviews() if r.id == review_id), None)

    def close_review(self, review_id, accepted):
        """Принято: попытка найдена. Отклонено: ссылка в чёрный список, повторный поиск через retry_days."""
        r = self.review(review_id)
        if not r:
            return None
        with self.db:
            self.db.execute("DELETE FROM review WHERE id = ?", (review_id,))
            if not accepted:
                self.db.execute("INSERT OR IGNORE INTO rejected (part_pk, kind, url) VALUES (?, ?, ?)",
                                (r.part_pk, r.kind, r.url or str(r.path)))
        self.record(r.part_pk, r.kind, "found" if accepted else "not_found", r.source, r.url)
        r.path.unlink(missing_ok=True)
        return r

    def rejected(self, part_pk, kind):
        return frozenset(u for (u,) in self.db.execute(
            "SELECT url FROM rejected WHERE part_pk = ? AND kind = ?", (part_pk, kind)))

    # --- настройки из бота

    def setting(self, key, default):
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return default if row is None else json.loads(row[0])

    def set_setting(self, key, value):
        if key not in SETTINGS:
            raise KeyError(key)
        with self.db:
            self.db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, json.dumps(value)))

    # --- история прогонов

    def start_run(self, trigger):
        with self.db:
            return self.db.execute("INSERT INTO runs (started, trigger) VALUES (?, ?)", (now(), trigger)).lastrowid

    def finish_run(self, run_id, summary):
        with self.db:
            self.db.execute("UPDATE runs SET finished = ?, summary = ? WHERE id = ?",
                            (now(), json.dumps(summary, ensure_ascii=False), run_id))

    def last_run(self):
        row = self.db.execute("SELECT started, finished, trigger, summary FROM runs "
                              "WHERE finished IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        return None if not row else {"started": row[0], "finished": row[1], "trigger": row[2], **json.loads(row[3])}

    def runs(self, limit=10):
        rows = self.db.execute("SELECT started, finished, trigger, summary FROM runs ORDER BY id DESC LIMIT ?",
                               (limit,)).fetchall()
        return [{"started": r[0], "finished": r[1], "trigger": r[2], **json.loads(r[3] or "{}")} for r in rows]
