"""Публикатор в Telegram-канал через Bot API sendPhoto.

Фото = сгенерированная карточка (локальный файл → multipart, либо публичный URL → form).
Идемпотентность: ключ опубликованного товара хранится в PublishState (SQLite) — повтор
не отправляем. Транзиентные сбои (сеть/5xx/429) — ретраим; ошибка TG (ok:false) → held
(не помечаем опубликованным, не ретраим). Троттлинг между постами — на уровне планировщика
(см. PublishState.seconds_since_last)."""
from __future__ import annotations
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
import httpx

TG_API = "https://api.telegram.org"
CAPTION_MAX = 1024


@dataclass
class PublishResult:
    ok: bool
    skipped: bool = False          # уже публиковали (идемпотентность)
    message_id: int | None = None
    error: str | None = None


@dataclass
class PublishedRec:
    """Запись опубликованного поста («живой канал» правит его по остаткам/ценам)."""
    key: str
    message_id: int | None
    channel: str
    price: int | None
    status: str                    # active | sold
    caption: str | None
    ts: float


class PublishState:
    """Анти-дубль: какие товары уже опубликованы (+ время последней публикации).
    Для «живого канала» хранит также канал/цену/статус/подпись поста."""
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._c() as c:
            c.execute("CREATE TABLE IF NOT EXISTS published "
                      "(key TEXT PRIMARY KEY, message_id INTEGER, ts REAL)")
            for ddl in ("ALTER TABLE published ADD COLUMN channel TEXT DEFAULT ''",
                        "ALTER TABLE published ADD COLUMN price INTEGER",
                        "ALTER TABLE published ADD COLUMN status TEXT DEFAULT 'active'",
                        "ALTER TABLE published ADD COLUMN caption TEXT"):
                try:
                    c.execute(ddl)
                except sqlite3.OperationalError:
                    pass                              # колонка уже есть

    def _c(self):
        return sqlite3.connect(self.path)

    def is_published(self, key: str) -> bool:
        with self._c() as c:
            return c.execute("SELECT 1 FROM published WHERE key=?", (key,)).fetchone() is not None

    def published_keys(self) -> set:
        """Все опубликованные ключи (для анти-дубля в планировщике)."""
        with self._c() as c:
            return {r[0] for r in c.execute("SELECT key FROM published").fetchall()}

    def mark(self, key: str, message_id: int | None, channel: str = "",
             caption: str | None = None) -> None:
        with self._c() as c:
            c.execute("INSERT OR REPLACE INTO published"
                      "(key, message_id, ts, channel, price, status, caption) "
                      "VALUES(?,?,?,?,NULL,'active',?)",
                      (key, message_id, time.time(), str(channel or ""), caption))

    def records(self) -> list[PublishedRec]:
        """Все посты для сверки «живого канала»."""
        with self._c() as c:
            rows = c.execute("SELECT key, message_id, channel, price, status, caption, ts "
                             "FROM published ORDER BY ts").fetchall()
        return [PublishedRec(key=r[0], message_id=r[1], channel=r[2] or "", price=r[3],
                             status=r[4] or "active", caption=r[5], ts=r[6] or 0.0)
                for r in rows]

    def update_sync(self, key: str, status: str | None = None, price: int | None = None,
                    caption: str | None = None) -> None:
        """Обновить только переданные поля записи (после правки поста в канале)."""
        sets, args = [], []
        for col, val in (("status", status), ("price", price), ("caption", caption)):
            if val is not None:
                sets.append(f"{col}=?")
                args.append(val)
        if not sets:
            return
        with self._c() as c:
            c.execute(f"UPDATE published SET {', '.join(sets)} WHERE key=?", (*args, key))

    def last_ts(self) -> float | None:
        with self._c() as c:
            row = c.execute("SELECT MAX(ts) FROM published").fetchone()
            return row[0] if row else None

    def seconds_since_last(self) -> float | None:
        ts = self.last_ts()
        return None if ts is None else max(0.0, time.time() - ts)


def send_message(bot_token: str, chat_id: str, text: str,
                 http: httpx.Client | None = None) -> bool:
    """Текстовое сообщение владельцу (алерты fail-ревизии/ошибок). Возвращает ok."""
    client = http or httpx.Client(timeout=30)
    try:
        r = client.post(f"{TG_API}/bot{bot_token}/sendMessage",
                        data={"chat_id": str(chat_id), "text": text})
        return bool((r.json() or {}).get("ok"))
    except (httpx.HTTPError, ValueError):
        return False


def edit_caption(bot_token: str, chat_id, message_id: int, caption: str, *,
                 parse_mode: str | None = None, http: httpx.Client | None = None,
                 retries: int = 1, backoff: float = 1.0):
    """editMessageCaption («живой канал»). → (ok, error, gone): gone=True — пост
    удалён/недоступен (больше не трогать: пометить sold). Транзиенты ретраим."""
    client = http or httpx.Client(timeout=30)
    data = {"chat_id": str(chat_id), "message_id": message_id,
            "caption": (caption or "")[:CAPTION_MAX]}
    if parse_mode:
        data["parse_mode"] = parse_mode
    for attempt in range(max(1, retries) + 1):
        try:
            r = client.post(f"{TG_API}/bot{bot_token}/editMessageCaption", data=data)
        except httpx.HTTPError as e:
            if attempt < retries:
                time.sleep(backoff)
                continue
            return False, f"network: {e}", False
        if r.status_code == 429 or r.status_code >= 500:
            if attempt < retries:
                time.sleep(backoff)
                continue
            return False, f"http {r.status_code}", False
        body = {}
        try:
            body = r.json() or {}
        except Exception:
            pass
        if body.get("ok"):
            return True, None, False
        desc = (body.get("description") or f"http {r.status_code}").lower()
        gone = ("not found" in desc or "message_id_invalid" in desc
                or "can't be edited" in desc)
        return False, desc, gone
    return False, "unknown", False


def _is_url(s: str) -> bool:
    return isinstance(s, str) and s.lower().startswith(("http://", "https://"))


def _send_once(bot_token, channel_id, image, caption, parse_mode, http, reply_markup=None):
    url = f"{TG_API}/bot{bot_token}/sendPhoto"
    data = {"chat_id": str(channel_id), "caption": caption}
    if parse_mode:
        data["parse_mode"] = parse_mode
    if reply_markup:                       # inline-кнопки (JSON-строка): ✅/❌ под превью
        data["reply_markup"] = reply_markup
    if _is_url(image):
        data["photo"] = image
        return http.post(url, data=data)
    with open(image, "rb") as fh:
        files = {"photo": (Path(image).name, fh.read(), "image/jpeg")}
    return http.post(url, data=data, files=files)


def publish_post(bot_token: str, channel_id: str, image: str, caption: str,
                 http: httpx.Client | None = None, *, parse_mode: str | None = None,
                 key: str | None = None, state: PublishState | None = None,
                 caption_max: int = CAPTION_MAX, retries: int = 1,
                 backoff: float = 1.0, reply_markup: str | None = None) -> PublishResult:
    """Опубликовать пост (sendPhoto). `image` — путь к файлу карточки или URL.
    `key`+`state` включают идемпотентность (повтор не публикуется).
    `reply_markup` — JSON inline-клавиатуры (кнопки ✅/❌ под превью на подтверждение)."""
    if state is not None and key and state.is_published(key):
        return PublishResult(ok=True, skipped=True)

    caption = (caption or "")[:caption_max]
    client = http or httpx.Client(timeout=60)

    last_err = None
    for attempt in range(max(1, retries) + 1):
        try:
            r = _send_once(bot_token, channel_id, image, caption, parse_mode, client,
                           reply_markup=reply_markup)
        except httpx.HTTPError as e:                       # сетевой сбой → ретрай
            last_err = f"network: {e}"
            if attempt < retries:
                time.sleep(backoff)
                continue
            return PublishResult(ok=False, error=last_err)

        if r.status_code == 429 or r.status_code >= 500:   # транзиент → ретрай
            last_err = f"http {r.status_code}"
            if attempt < retries:
                time.sleep(backoff)
                continue
            return PublishResult(ok=False, error=last_err)

        body = {}
        try:
            body = r.json() or {}
        except Exception:
            pass
        if r.status_code != 200 or not body.get("ok"):     # ошибка TG → held (не ретраим)
            return PublishResult(ok=False, error=body.get("description") or f"http {r.status_code}")

        mid = (body.get("result") or {}).get("message_id")
        if state is not None and key:
            state.mark(key, mid, channel=str(channel_id), caption=caption)
        return PublishResult(ok=True, message_id=mid)

    return PublishResult(ok=False, error=last_err or "unknown")
