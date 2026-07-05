"""Кнопка «📩 Заказать» под постами канала (волна 1б). Клик ведёт на бота
(`t.me/<bot>?start=ord_<code>`): клиенту — карточка товара, владельцу — лид.
Код короткий и детерминированный (sha1 от ключа): start-параметр Telegram
допускает только [A-Za-z0-9_-] и ≤64 символов, наши ключи туда не влезают."""
from __future__ import annotations
import hashlib
import html
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

_TAG_RE = re.compile(r"<[^>]+>")


def _plain(s: str) -> str:
    """Строку подписи (HTML) → обычный текст: убрать теги, раскрыть сущности.
    Подпись канала — в HTML (blockquote/b, экранированные <артикулы>), а клиенту
    и в лид шлём обычным текстом (sendMessage без parse_mode)."""
    return html.unescape(_TAG_RE.sub("", s or "")).strip()


@dataclass
class Lead:
    ts: float
    user_id: int
    username: str
    key: str
    qty: int = 1
    comment: str = ""
    phone: str = ""


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
            for ddl in ("ALTER TABLE leads ADD COLUMN qty INTEGER DEFAULT 1",
                        "ALTER TABLE leads ADD COLUMN comment TEXT DEFAULT ''",
                        "ALTER TABLE leads ADD COLUMN phone TEXT DEFAULT ''"):
                try:
                    c.execute(ddl)
                except sqlite3.OperationalError:
                    pass                              # колонка уже есть

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

    def add_lead(self, user_id: int, username: str, key: str,
                 qty: int = 1, comment: str = "", phone: str = "") -> None:
        with self._c() as c:
            c.execute("INSERT INTO leads(ts, user_id, username, key, qty, comment, phone) "
                      "VALUES(?,?,?,?,?,?,?)",
                      (time.time(), user_id, username or "", key, int(qty),
                       comment or "", phone or ""))

    def leads(self) -> list[Lead]:
        with self._c() as c:
            rows = c.execute("SELECT ts, user_id, username, key, qty, comment, phone "
                             "FROM leads ORDER BY ts").fetchall()
        return [Lead(*r) for r in rows]


def order_markup(order_bot: str, code: str) -> str:
    """JSON inline-клавиатуры с url-кнопкой «Заказать» (для publish_post)."""
    return json.dumps({"inline_keyboard": [[
        {"text": "📩 Заказать", "url": f"https://t.me/{order_bot}?start=ord_{code}"}]]},
        ensure_ascii=False)


def item_summary(pub_state, key: str) -> str:
    """Название + строка цены из сохранённой подписи поста, очищённые от HTML
    (подпись канала — в HTML; клиенту/в лид шлём обычным текстом)."""
    for r in pub_state.records():
        if r.key == key and r.caption:
            lines = r.caption.splitlines()
            return "\n".join(_plain(ln) for ln in lines[:2])
    return key
