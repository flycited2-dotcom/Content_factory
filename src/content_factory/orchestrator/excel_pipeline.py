"""Конвейер excel-товаров (источник «прайс владельца», подпроект 3):
new → research (УТП+фото по наименованию; кэш — ChatGPT не дёргается повторно)
→ card (kbt-карточка из research-фото) → preview (превью с ценой в ревью-канал).
Дальше — штатные кнопки ✅/❌/🔄. Чистая логика: сеть/файлы инъецируются
(обвязка — excel_run). Ретрай одного этапа: 1 повтор, потом failed."""
from __future__ import annotations
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

MAX_TRIES = 2                       # попыток на этап (сабмит + 1 ретрай)


@dataclass
class ExcelItem:
    key: str
    brand: str
    model: str
    name: str
    price: int
    status: str                     # new | research | card | preview | failed
    research_job: int | None
    card_job: int | None
    tries: int
    error: str | None


class ExcelStore:
    """Состояние excel-товаров + кэш research-результатов (state.db)."""
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._c() as c:
            c.execute("CREATE TABLE IF NOT EXISTS excel_items ("
                      "key TEXT PRIMARY KEY, brand TEXT, model TEXT, name TEXT, "
                      "price INTEGER, status TEXT DEFAULT 'new', research_job INTEGER, "
                      "card_job INTEGER, tries INTEGER DEFAULT 0, error TEXT, ts REAL, "
                      "due_at REAL)")
            try:      # миграция существующей таблицы (SQLite: колонку — только ALTER)
                c.execute("ALTER TABLE excel_items ADD COLUMN due_at REAL")
            except sqlite3.OperationalError:
                pass                       # колонка уже есть
            c.execute("CREATE TABLE IF NOT EXISTS research_cache ("
                      "model_key TEXT PRIMARY KEY, utp TEXT, photo_path TEXT, "
                      "source TEXT DEFAULT 'research', ts REAL)")

    def _c(self):
        return sqlite3.connect(self.path)

    def add_items(self, rows, due_at: float | None = None) -> int:
        """rows: [(key, brand, model, name, price)]. Повторные ключи игнорируются.
        due_at — расписание (/task «завтра 9:00»): до срока тик товар не берёт."""
        n = 0
        with self._c() as c:
            for key, brand, model, name, price in rows:
                cur = c.execute("INSERT OR IGNORE INTO excel_items"
                                "(key, brand, model, name, price, status, tries, ts, due_at) "
                                "VALUES(?,?,?,?,?,'new',0,?,?)",
                                (key, brand, model, name, price, time.time(), due_at))
                n += cur.rowcount
        return n

    def _row(self, r) -> ExcelItem:
        return ExcelItem(key=r[0], brand=r[1], model=r[2], name=r[3], price=r[4],
                         status=r[5], research_job=r[6], card_job=r[7],
                         tries=r[8] or 0, error=r[9])

    def all_keys(self) -> set:
        """Все ключи в работе/истории (анти-дубль при /make)."""
        with self._c() as c:
            return {r[0] for r in c.execute("SELECT key FROM excel_items").fetchall()}

    def by_status(self, status: str, now: float | None = None) -> list[ExcelItem]:
        # new с due_at в будущем скрыты от тика (расписание /task); остальные
        # статусы расписание не фильтрует — товар уже в работе.
        # now — инжект часов для тестов, по умолчанию реальное время
        due_filter = " AND (due_at IS NULL OR due_at <= ?)" if status == "new" else ""
        args = (status, now if now is not None else time.time()) if status == "new" else (status,)
        with self._c() as c:
            rows = c.execute("SELECT key, brand, model, name, price, status, research_job, "
                             f"card_job, tries, error FROM excel_items WHERE status=?{due_filter} "
                             "ORDER BY ts", args).fetchall()
        return [self._row(r) for r in rows]

    def update(self, key: str, **fields) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._c() as c:
            c.execute(f"UPDATE excel_items SET {sets} WHERE key=?",
                      (*fields.values(), key))

    def retry_failed(self) -> int:
        """Вернуть все failed-позиции в конвейер с чистого листа (status new,
        tries 0, error снят) — команда /excel retry (запрос владельца 2026-07-07:
        «research без фото»/таймауты не терять, а перезапускать одной командой)."""
        with self._c() as c:
            cur = c.execute("UPDATE excel_items SET status='new', tries=0, "
                            "error=NULL, research_job=NULL, card_job=NULL "
                            "WHERE status='failed'")
            return cur.rowcount

    # ── кэш research (волна 1в): повторные модели не дёргают ChatGPT ──────────
    def cache_get(self, model_key: str):
        with self._c() as c:
            row = c.execute("SELECT utp, photo_path FROM research_cache WHERE model_key=?",
                            (model_key,)).fetchone()
        return row if row else None

    def cache_put(self, model_key: str, utp: str, photo_path: str | None,
                  source: str = "research") -> None:
        with self._c() as c:
            # ручные УТП (source='manual') приоритетнее — research их не перезаписывает
            row = c.execute("SELECT source FROM research_cache WHERE model_key=?",
                            (model_key,)).fetchone()
            if row and row[0] == "manual" and source != "manual":
                return
            c.execute("INSERT OR REPLACE INTO research_cache"
                      "(model_key, utp, photo_path, source, ts) VALUES(?,?,?,?,?)",
                      (model_key, utp, photo_path, source, time.time()))


def _cache_key(item: ExcelItem) -> str:
    return f"{item.brand.strip().lower()}|{item.model.strip().lower()}"


def _category_word(item: ExcelItem) -> str:
    """Слово-категория для research-промпта: часть наименования до бренда
    («Холодильник Beko …» → «Холодильник»)."""
    low = item.name.lower()
    pos = low.find(item.brand.lower())
    head = item.name[:pos].strip() if pos > 0 else ""
    return head or item.name.split()[0]


def tick(store: ExcelStore, submit_research, read_job, submit_card, preview) -> dict:
    """Один проход конвейера. Инъекции:
    submit_research(brand, model, category) -> job_id
    read_job(job_id) -> (status, output_filename, result_specs, error)
    submit_card(brand, model, utp, photo_path) -> job_id
    preview(item, card_output_filename) -> bool
    """
    stats = {"research": 0, "card": 0, "preview": 0, "failed": 0}

    def _fail_or_retry(item, stage_reset: dict, err: str):
        if item.tries + 1 >= MAX_TRIES:
            store.update(item.key, status="failed", error=err)
            stats["failed"] += 1
        else:
            store.update(item.key, tries=item.tries + 1, **stage_reset)

    for item in store.by_status("new"):
        cached = store.cache_get(_cache_key(item))
        if cached:
            utp, photo = cached
            if photo:
                job = submit_card(item.brand, item.model, utp, photo)
                store.update(item.key, status="card", card_job=job, tries=0)
                stats["card"] += 1
                continue
        job = submit_research(item.brand, item.model, _category_word(item))
        store.update(item.key, status="research", research_job=job)
        stats["research"] += 1

    for item in store.by_status("research"):
        status, out, utp, err = read_job(item.research_job)
        if status == "done":
            store.cache_put(_cache_key(item), utp or "", out)
            if not out:                     # фото не нашлось — карточку не из чего делать
                _fail_or_retry(item, {"status": "new", "research_job": None},
                               "research без фото")
                continue
            job = submit_card(item.brand, item.model, utp or "", out)
            store.update(item.key, status="card", card_job=job, tries=0)
            stats["card"] += 1
        elif status == "failed":
            _fail_or_retry(item, {"status": "new", "research_job": None},
                           f"research: {err or 'ошибка'}")

    for item in store.by_status("card"):
        status, out, _, err = read_job(item.card_job)
        if status == "done" and out:
            if preview(item, out):
                store.update(item.key, status="preview")
                stats["preview"] += 1
        elif status == "failed":
            _fail_or_retry(item, {"status": "new", "card_job": None},
                           f"card: {err or 'ошибка'}")
    return stats
