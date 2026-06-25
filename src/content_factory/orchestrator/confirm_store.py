"""Очередь подтверждений (confirm-пилот). Когда задача с confirm=True и пост прошёл ревизию,
он НЕ публикуется сразу — кладётся сюда и шлётся превью владельцу. По команде /approve <key>
бот публикует пост в канал, по /reject <key> — отклоняет. Так пилот идёт в боевой канал
безопасно (каждый пост — явный OK владельца)."""
from __future__ import annotations
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Awaiting:
    key: str
    channel: str
    card_path: str
    caption: str
    status: str          # pending | published | rejected
    ts: float = 0.0


class ConfirmStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._c() as c:
            c.execute("CREATE TABLE IF NOT EXISTS awaiting ("
                      "key TEXT PRIMARY KEY, channel TEXT, card_path TEXT, caption TEXT, "
                      "status TEXT DEFAULT 'pending', ts REAL)")

    def _c(self):
        return sqlite3.connect(self.path)

    def add(self, key: str, channel: str, card_path: str, caption: str) -> None:
        """Поставить пост на подтверждение (upsert → статус сбрасывается в pending)."""
        with self._c() as c:
            c.execute("INSERT INTO awaiting(key, channel, card_path, caption, status, ts) "
                      "VALUES(?,?,?,?, 'pending', ?) "
                      "ON CONFLICT(key) DO UPDATE SET channel=excluded.channel, "
                      "card_path=excluded.card_path, caption=excluded.caption, "
                      "status='pending', ts=excluded.ts",
                      (key, channel, card_path, caption, time.time()))

    def get(self, key: str) -> Awaiting | None:
        with self._c() as c:
            r = c.execute("SELECT key, channel, card_path, caption, status, ts "
                          "FROM awaiting WHERE key=?", (key,)).fetchone()
        return Awaiting(*r) if r else None

    def list_pending(self) -> list[Awaiting]:
        with self._c() as c:
            rows = c.execute("SELECT key, channel, card_path, caption, status, ts "
                             "FROM awaiting WHERE status='pending' ORDER BY ts").fetchall()
        return [Awaiting(*r) for r in rows]

    def mark(self, key: str, status: str) -> None:
        with self._c() as c:
            c.execute("UPDATE awaiting SET status=? WHERE key=?", (status, key))
