"""Состояние диалога заказа клиента (задача 3, выбор владельца 2026-07-05).
Клиент по кнопке «Заказать» проходит шаги: кол-во → (опц. своё число) →
комментарий → заявка (лид в отдельный чат). Чистая логика — в bot/order_flow.py;
здесь только стор в SQLite (как WizardStore), ключ — chat_id клиента."""
from __future__ import annotations
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

STEPS = ("awaiting_qty", "awaiting_qty_custom", "awaiting_comment", "awaiting_phone")


@dataclass
class OrderState:
    chat_id: str
    step: str
    key: str
    qty: int | None
    comment: str | None = None
    origin: str = ""
    content_id: str = ""


# Диалог без движения дольше этого срока считается заброшенным. Без срока открытый
# вчера опросник перехватывал любой следующий текст владельца или клиента.
DEFAULT_TTL_SECONDS = 1800


class OrderDialogStore:
    def __init__(self, path, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self.ttl_seconds = int(ttl_seconds)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._c() as c:
            c.execute("CREATE TABLE IF NOT EXISTS order_dialog ("
                      "chat_id TEXT PRIMARY KEY, step TEXT, key TEXT, qty INTEGER, "
                      "comment TEXT, origin TEXT DEFAULT '', content_id TEXT DEFAULT '')")
            for ddl in ("ALTER TABLE order_dialog ADD COLUMN comment TEXT",
                        "ALTER TABLE order_dialog ADD COLUMN origin TEXT DEFAULT ''",
                        "ALTER TABLE order_dialog ADD COLUMN content_id TEXT DEFAULT ''",
                        "ALTER TABLE order_dialog ADD COLUMN updated_at REAL DEFAULT 0"):
                try:
                    c.execute(ddl)
                except sqlite3.OperationalError:
                    pass

    def _c(self):
        return sqlite3.connect(self.path)

    def start(self, chat_id: str, key: str, *, origin: str = "",
              content_id: str = "") -> None:
        """Начать (или перезапустить с нуля) заявку по товару key."""
        with self._c() as c:
            c.execute("INSERT INTO order_dialog(chat_id,step,key,qty,comment,origin,content_id,"
                      "updated_at) VALUES(?,?,?,NULL,NULL,?,?,?) "
                      "ON CONFLICT(chat_id) DO UPDATE SET step=excluded.step, "
                      "key=excluded.key,qty=NULL,comment=NULL,origin=excluded.origin,"
                      "content_id=excluded.content_id,updated_at=excluded.updated_at",
                      (str(chat_id), "awaiting_qty", key, origin or "", content_id or "",
                       time.time()))

    def set_qty(self, chat_id: str, qty: int) -> None:
        with self._c() as c:
            c.execute("UPDATE order_dialog SET qty=?, step=?, updated_at=? WHERE chat_id=?",
                      (int(qty), "awaiting_comment", time.time(), str(chat_id)))

    def set_comment(self, chat_id: str, comment: str) -> None:
        with self._c() as c:
            c.execute("UPDATE order_dialog SET comment=?, step=?, updated_at=? WHERE chat_id=?",
                      (comment or "", "awaiting_phone", time.time(), str(chat_id)))

    def set_step(self, chat_id: str, step: str) -> None:
        with self._c() as c:
            c.execute("UPDATE order_dialog SET step=?, updated_at=? WHERE chat_id=?",
                      (step, time.time(), str(chat_id)))

    def cancel(self, chat_id: str) -> None:
        with self._c() as c:
            c.execute("DELETE FROM order_dialog WHERE chat_id=?", (str(chat_id),))

    def snapshot(self, chat_id: str) -> OrderState | None:
        with self._c() as c:
            row = c.execute("SELECT step,key,qty,comment,COALESCE(origin,''),"
                            "COALESCE(content_id,''),COALESCE(updated_at,0) FROM order_dialog "
                            "WHERE chat_id=?", (str(chat_id),)).fetchone()
        if not row:
            return None
        step, key, qty, comment, origin, content_id, updated_at = row
        if time.time() - float(updated_at) > self.ttl_seconds:
            self.cancel(chat_id)                      # заброшенный диалог не перехватывает текст
            return None
        return OrderState(chat_id=str(chat_id), step=step, key=key, qty=qty,
                          comment=comment, origin=origin, content_id=content_id)

    def expire_for_test(self, chat_id: str, seconds_ago: int) -> None:
        """Состарить диалог — только для тестов срока жизни."""
        with self._c() as c:
            c.execute("UPDATE order_dialog SET updated_at=? WHERE chat_id=?",
                      (time.time() - int(seconds_ago), str(chat_id)))
