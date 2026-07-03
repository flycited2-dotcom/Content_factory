"""Кнопка «📩 Заказать» под постами канала (волна 1б). Клик ведёт на бота
(`t.me/<bot>?start=ord_<code>`): клиенту — карточка товара, владельцу — лид.
Код короткий и детерминированный (sha1 от ключа): start-параметр Telegram
допускает только [A-Za-z0-9_-] и ≤64 символов, наши ключи туда не влезают."""
from __future__ import annotations
import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Lead:
    ts: float
    user_id: int
    username: str
    key: str


class OrderLinks:
    """code ↔ key товара + журнал лидов (state.db)."""
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._c() as c:
            c.execute("CREATE TABLE IF NOT EXISTS order_links "
                      "(code TEXT PRIMARY KEY, key TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS leads "
                      "(ts REAL, user_id INTEGER, username TEXT, key TEXT)")

    def _c(self):
        return sqlite3.connect(self.path)

    def code_for(self, key: str) -> str:
        code = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
        with self._c() as c:
            c.execute("INSERT OR IGNORE INTO order_links(code, key) VALUES(?,?)", (code, key))
        return code

    def key_for(self, code: str) -> str | None:
        with self._c() as c:
            row = c.execute("SELECT key FROM order_links WHERE code=?", (code,)).fetchone()
        return row[0] if row else None

    def add_lead(self, user_id: int, username: str, key: str) -> None:
        with self._c() as c:
            c.execute("INSERT INTO leads(ts, user_id, username, key) VALUES(?,?,?,?)",
                      (time.time(), user_id, username or "", key))

    def leads(self) -> list[Lead]:
        with self._c() as c:
            rows = c.execute("SELECT ts, user_id, username, key FROM leads ORDER BY ts").fetchall()
        return [Lead(*r) for r in rows]


def order_markup(order_bot: str, code: str) -> str:
    """JSON inline-клавиатуры с url-кнопкой «Заказать» (для publish_post)."""
    return json.dumps({"inline_keyboard": [[
        {"text": "📩 Заказать", "url": f"https://t.me/{order_bot}?start=ord_{code}"}]]},
        ensure_ascii=False)


def _item_summary(pub_state, key: str) -> str:
    """Название + строка цены из сохранённой подписи поста."""
    for r in pub_state.records():
        if r.key == key and r.caption:
            lines = r.caption.splitlines()
            return "\n".join(lines[:2])
    return key


def handle_order_start(text: str, user: dict, links: OrderLinks, pub_state) -> tuple:
    """/start ord_<code> от клиента → (ответ клиенту, лид владельцу | None).
    Не заказ (обычный /start и пр.) → (None, None) — бот работает как раньше."""
    parts = (text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].startswith("ord_") or not parts[0].startswith("/start"):
        return None, None
    key = links.key_for(parts[1][4:].strip())
    if not key:
        return "К сожалению, товар не найден (возможно, пост устарел). Напишите нам!", None
    summary = _item_summary(pub_state, key)
    uname = user.get("username") or ""
    who = (f"@{uname}" if uname else "") or user.get("first_name") or str(user.get("id"))
    links.add_lead(int(user.get("id") or 0), uname, key)
    reply = (f"Спасибо за заявку! 👍\n\nВы выбрали:\n{summary}\n\n"
             f"Менеджер свяжется с вами в ближайшее время.")
    lead = f"📩 ЛИД: {who} (id {user.get('id')})\n{summary}"
    return reply, lead
