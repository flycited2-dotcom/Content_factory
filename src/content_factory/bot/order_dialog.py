"""Состояние диалога заказа клиента (задача 3, выбор владельца 2026-07-05).
Клиент по кнопке «Заказать» проходит шаги: кол-во → (опц. своё число) →
комментарий → заявка (лид в отдельный чат). Чистая логика — в bot/order_flow.py;
здесь только стор в SQLite (как WizardStore), ключ — chat_id клиента."""
from __future__ import annotations
import sqlite3
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


class OrderDialogStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._c() as c:
            c.execute("CREATE TABLE IF NOT EXISTS order_dialog ("
                      "chat_id TEXT PRIMARY KEY, step TEXT, key TEXT, qty INTEGER, "
                      "comment TEXT)")

    def _c(self):
        return sqlite3.connect(self.path)

    def start(self, chat_id: str, key: str) -> None:
        """Начать (или перезапустить с нуля) заявку по товару key."""
        with self._c() as c:
            c.execute("INSERT INTO order_dialog(chat_id, step, key, qty, comment) "
                      "VALUES(?,?,?,NULL,NULL) "
                      "ON CONFLICT(chat_id) DO UPDATE SET step=excluded.step, "
                      "key=excluded.key, qty=NULL, comment=NULL",
                      (str(chat_id), "awaiting_qty", key))

    def set_qty(self, chat_id: str, qty: int) -> None:
        with self._c() as c:
            c.execute("UPDATE order_dialog SET qty=?, step=? WHERE chat_id=?",
                      (int(qty), "awaiting_comment", str(chat_id)))

    def set_comment(self, chat_id: str, comment: str) -> None:
        with self._c() as c:
            c.execute("UPDATE order_dialog SET comment=?, step=? WHERE chat_id=?",
                      (comment or "", "awaiting_phone", str(chat_id)))

    def set_step(self, chat_id: str, step: str) -> None:
        with self._c() as c:
            c.execute("UPDATE order_dialog SET step=? WHERE chat_id=?",
                      (step, str(chat_id)))

    def cancel(self, chat_id: str) -> None:
        with self._c() as c:
            c.execute("DELETE FROM order_dialog WHERE chat_id=?", (str(chat_id),))

    def snapshot(self, chat_id: str) -> OrderState | None:
        with self._c() as c:
            row = c.execute("SELECT step, key, qty, comment FROM order_dialog "
                            "WHERE chat_id=?", (str(chat_id),)).fetchone()
        if not row:
            return None
        step, key, qty, comment = row
        return OrderState(chat_id=str(chat_id), step=step, key=key, qty=qty,
                          comment=comment)
