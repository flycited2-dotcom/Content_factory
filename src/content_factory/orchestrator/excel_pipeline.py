"""Конвейер excel-товаров (источник «прайс владельца», подпроект 3):
new → research (УТП+фото по наименованию; кэш — ChatGPT не дёргается повторно)
→ card (kbt-карточка из research-фото) → preview (превью с ценой в ревью-канал).
Дальше — штатные кнопки ✅/❌/🔄. Чистая логика: сеть/файлы инъецируются
(обвязка — excel_run). Ретрай одного этапа: 1 повтор, потом failed."""
from __future__ import annotations
import json
import logging
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

MAX_TRIES = 2                       # попыток на этап (сабмит + 1 ретрай)
_LOG = logging.getLogger(__name__)


@dataclass
class ExcelItem:
    key: str
    brand: str
    model: str
    name: str
    price: int
    status: str                     # new | research | card | preview | failed | cancelled | submission
    research_job: int | None
    card_job: int | None
    tries: int
    error: str | None
    card_mode: str = "kbt"


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
                      "due_at REAL, card_mode TEXT DEFAULT 'kbt')")
            for migration in (
                "ALTER TABLE excel_items ADD COLUMN due_at REAL",
                "ALTER TABLE excel_items ADD COLUMN card_mode TEXT DEFAULT 'kbt'",
                "ALTER TABLE excel_items ADD COLUMN research_request_key TEXT",
                "ALTER TABLE excel_items ADD COLUMN card_request_key TEXT",
            ):
                try:
                    c.execute(migration)
                except sqlite3.OperationalError:
                    pass
            c.execute("CREATE TABLE IF NOT EXISTS research_cache ("
                      "model_key TEXT PRIMARY KEY, utp TEXT, photo_path TEXT, "
                      "source TEXT DEFAULT 'research', ts REAL, evidence TEXT)")
            try:
                c.execute("ALTER TABLE research_cache ADD COLUMN evidence TEXT")
            except sqlite3.OperationalError:
                pass
            c.execute("CREATE TABLE IF NOT EXISTS excel_selections ("
                      "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, "
                      "keys_json TEXT, requested INTEGER)")

    @contextmanager
    def _c(self):
        # sqlite3.Connection.__exit__ делает commit/rollback, но НЕ закрывает
        # соединение. Один ready-price тик обращается к сотням строк и раньше
        # оставлял сотни fd до завершения процесса, вызывая долгий fdatasync и
        # блокировку state DB. Закрываем каждое соединение явно.
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def add_items(self, rows, due_at: float | None = None) -> int:
        """rows: [(key, brand, model, name, price)]. Повторные ключи игнорируются.
        due_at — расписание (/task «завтра 9:00»): до срока тик товар не берёт."""
        n = 0
        with self._c() as c:
            for row in rows:
                key, brand, model, name, price = row[:5]
                card_mode = row[5] if len(row) > 5 else "kbt"
                cur = c.execute("INSERT OR IGNORE INTO excel_items"
                                "(key, brand, model, name, price, status, tries, ts, due_at, card_mode) "
                                "VALUES(?,?,?,?,?,'new',0,?,?,?)",
                                (key, brand, model, name, price, time.time(), due_at, card_mode))
                n += cur.rowcount
        return n

    def _row(self, r) -> ExcelItem:
        return ExcelItem(key=r[0], brand=r[1], model=r[2], name=r[3], price=r[4],
                         status=r[5], research_job=r[6], card_job=r[7],
                         tries=r[8] or 0, error=r[9], card_mode=r[10] or "kbt")

    def all_keys(self) -> set:
        """Все ключи в работе/истории (анти-дубль при /make)."""
        with self._c() as c:
            return {r[0] for r in c.execute("SELECT key FROM excel_items").fetchall()}

    @staticmethod
    def _selection_jobs(queue_db, job_ids: set) -> dict:
        """Read linked jobs without creating or modifying the shared queue.

        Missing or unreadable jobs remain absent from this result: a cancelled
        item with such a reference cannot safely be submitted again.
        """
        if not job_ids or not queue_db:
            return {}
        queue_path = Path(queue_db)
        if not queue_path.is_file():
            return {}
        connection = None
        try:
            connection = sqlite3.connect(
                queue_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
            if not {"id", "status"} <= columns:
                return {}
            output = "output_filename" if "output_filename" in columns else "NULL"
            ids = list(job_ids)
            jobs = {}
            for start in range(0, len(ids), 900):
                batch = ids[start:start + 900]
                marks = ",".join("?" for _ in batch)
                jobs.update({row[0]: (row[1], row[2]) for row in connection.execute(
                    f"SELECT id,status,{output} FROM jobs WHERE id IN ({marks})", batch)})
            return jobs
        except (OSError, sqlite3.Error, ValueError):
            return {}
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _cancelled_selection_stage(row, jobs) -> tuple | None:
        """Choose a safe stage, retaining completed work rather than buying it again."""
        key, status, research_job, card_job = row[:4]
        if status != "cancelled" or key.startswith("ready-price|"):
            return None
        if any(job_id is None and request_key for job_id, request_key
               in zip(row[2:4], row[4:6])):
            # A timeout can happen after the queue accepted a paid request but
            # before its ID was persisted. Reusing another photo is unsafe.
            return None
        for job_id in (research_job, card_job):
            if job_id is None:
                continue
            linked = jobs.get(job_id)
            if linked is None or linked[0] not in {"done", "failed", "cancelled"}:
                return None
            if linked[0] == "done" and not str(linked[1] or "").strip():
                return None
        research_done = research_job is not None and jobs[research_job][0] == "done"
        card_done = card_job is not None and jobs[card_job][0] == "done"
        if card_done:
            return "card", research_job if research_done else None, card_job
        if research_done:
            return "research", research_job, None
        return "new", None, None

    @staticmethod
    def _published_selection_keys(connection) -> set:
        """An old list must not override a preview decision or a published post."""
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        blocked = set()
        if "published" in tables:
            blocked.update(row[0] for row in connection.execute("SELECT key FROM published"))
        if "awaiting" in tables:
            blocked.update(row[0] for row in connection.execute(
                "SELECT key FROM awaiting WHERE status IN ('pending','rejected','published')"))
        return blocked

    def selection_blocked_keys(self, queue_db=None) -> set:
        """Keys unavailable to a new owner selection, preserving the full history.

        Only cancelled manual items whose linked work is provably terminal are
        selectable again. Avito batches use their own restart and selection rules.
        """
        with self._c() as c:
            rows = c.execute(
                "SELECT key,status,research_job,card_job,research_request_key,"
                "card_request_key FROM excel_items").fetchall()
            published = self._published_selection_keys(c)
        job_ids = {job_id for row in rows
                   if row[1] == "cancelled" and not row[0].startswith("ready-price|")
                   for job_id in row[2:4] if job_id is not None}
        jobs = self._selection_jobs(queue_db, job_ids)
        return published | {row[0] for row in rows
                            if self._cancelled_selection_stage(row, jobs) is None}

    def select_items(self, rows, due_at: float | None = None, queue_db=None,
                     reservation: bool = False) -> list[tuple]:
        """Atomically accept only the owner's selected manual keys, once.

        ``add_items`` remains an import operation that never resurrects history.
        This explicit selection may restore safe cancelled items and updates their
        current price. ``reservation`` claims photo overrides as ``submission``:
        the ordinary tick cannot pick them up during a network request. A failed
        or abandoned submission must remain blocked until explicitly reconciled;
        it must never silently fall through to research or a timer-based retry.
        Completed linked jobs resume their existing stage even for an override.
        """
        requested_rows = [tuple(row) for row in rows]
        if any(len(row) < 5 for row in requested_rows):
            raise ValueError("selection rows require key, brand, model, name and price")
        unique = []
        seen = set()
        for row in requested_rows:
            if row[0] not in seen:
                unique.append(row)
                seen.add(row[0])
        accepted = []
        with self._c() as c:
            c.execute("BEGIN IMMEDIATE")
            blocked = self._published_selection_keys(c)
            existing = {}
            for row in unique:
                key = row[0]
                if key.startswith("ready-price|") or key in blocked:
                    continue
                existing[key] = c.execute(
                    "SELECT key,status,research_job,card_job,research_request_key,"
                    "card_request_key FROM excel_items WHERE key=?", (key,)).fetchone()
            job_ids = {job_id for old in existing.values()
                       if old is not None and old[1] == "cancelled"
                       for job_id in old[2:4] if job_id is not None}
            jobs = self._selection_jobs(queue_db, job_ids)
            ts = time.time()
            for row in unique:
                key, brand, model, name, price = row[:5]
                if key not in existing:
                    continue
                old = existing[key]
                stage = (("new", None, None) if old is None
                         else self._cancelled_selection_stage(old, jobs))
                if stage is None:
                    continue
                status, research_job, card_job = stage
                if reservation and status == "new":
                    status = "submission"
                card_mode = row[5] if len(row) > 5 else "kbt"
                if old is None:
                    c.execute(
                        "INSERT INTO excel_items(key,brand,model,name,price,status,tries,"
                        "ts,due_at,card_mode) VALUES(?,?,?,?,?,?,0,?,?,?)",
                        (key, brand, model, name, price, status, ts, due_at, card_mode))
                else:
                    c.execute(
                        "UPDATE excel_items SET brand=?,model=?,name=?,price=?,status=?,"
                        "research_job=?,card_job=?,tries=0,error=NULL,ts=?,due_at=?,"
                        "card_mode=?,research_request_key=?,card_request_key=? WHERE key=?",
                        (brand, model, name, price, status, research_job, card_job,
                         ts, due_at, card_mode,
                         old[4] if research_job is not None else None,
                         old[5] if card_job is not None else None, key))
                accepted.append(row)
            if accepted:
                c.execute("INSERT INTO excel_selections(ts,keys_json,requested) VALUES(?,?,?)",
                          (ts, json.dumps([row[0] for row in accepted]), len(requested_rows)))
        return accepted

    def latest_selection(self) -> dict | None:
        """Actual accepted keys from the last owner action, independent of older work."""
        with self._c() as c:
            row = c.execute("SELECT id,ts,requested,keys_json FROM excel_selections "
                            "ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        try:
            keys = json.loads(row[3])
        except (TypeError, ValueError):
            return None
        if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
            return None
        return {"id": row[0], "ts": row[1], "requested": row[2], "keys": keys}

    def get(self, key: str) -> ExcelItem | None:
        with self._c() as c:
            r = c.execute("SELECT key, brand, model, name, price, status, research_job, "
                          "card_job, tries, error,card_mode FROM excel_items WHERE key=?",
                          (key,)).fetchone()
        return self._row(r) if r else None

    def by_status(self, status: str, now: float | None = None) -> list[ExcelItem]:
        # new с due_at в будущем скрыты от тика (расписание /task); остальные
        # статусы расписание не фильтрует — товар уже в работе.
        # now — инжект часов для тестов, по умолчанию реальное время
        due_filter = " AND (due_at IS NULL OR due_at <= ?)" if status == "new" else ""
        args = (status, now if now is not None else time.time()) if status == "new" else (status,)
        with self._c() as c:
            rows = c.execute("SELECT key, brand, model, name, price, status, research_job, "
                             f"card_job, tries, error,card_mode FROM excel_items WHERE status=?{due_filter} "
                             "ORDER BY ts", args).fetchall()
        return [self._row(r) for r in rows]

    def _sched_rows(self, cmp: str, now: float | None) -> list[dict]:
        with self._c() as c:
            rows = c.execute("SELECT key, brand, model, name, due_at FROM excel_items "
                             "WHERE status='new' AND due_at IS NOT NULL "
                             f"AND due_at {cmp} ? ORDER BY due_at",
                             (now if now is not None else time.time(),)).fetchall()
        return [{"key": r[0], "brand": r[1], "model": r[2], "name": r[3],
                 "due_at": r[4]}
                for r in rows]

    def scheduled(self, now: float | None = None) -> list[dict]:
        """new-позиции с due_at в будущем (расписание /task) — для /excel:
        by_status('new') их прячет от тика, и в статусе они были невидимы
        («полный вакуум информации», жалоба 2026-07-10)."""
        return self._sched_rows(">", now)

    def due_scheduled(self, now: float | None = None) -> list[dict]:
        """Дозревшие отложенные new-позиции — их заберёт ближайший тик; excel_run
        по этому списку шлёт владельцу «стартовала отложенная генерация» (после
        тика они уже research → алерт одноразовый)."""
        return self._sched_rows("<=", now)

    def update(self, key: str, **fields) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._c() as c:
            c.execute(f"UPDATE excel_items SET {sets} WHERE key=?",
                      (*fields.values(), key))

    def update_if_status(self, key: str, expected_status: str, **fields) -> bool:
        """Advance a snapshot only while it still has its original stage."""
        if not fields:
            return False
        sets = ", ".join(f"{column}=?" for column in fields)
        with self._c() as c:
            return bool(c.execute(
                f"UPDATE excel_items SET {sets} WHERE key=? AND status=?",
                (*fields.values(), key, expected_status)).rowcount)

    def get_or_create_request_key(self, key: str, stage: str) -> str:
        """Persist the submission identity before a network request can time out."""
        if stage not in {"research", "card"}:
            raise ValueError("unknown submission stage")
        column = f"{stage}_request_key"
        with self._c() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(f"SELECT {column} FROM excel_items WHERE key=?", (key,)).fetchone()
            if row is None:
                raise KeyError(key)
            if row[0]:
                return str(row[0])
            request_key = uuid.uuid4().hex
            c.execute(f"UPDATE excel_items SET {column}=? WHERE key=?", (request_key, key))
            return request_key

    def bind_submitted_job(self, key: str, stage: str, job: int,
                           expected_status: str, tries: int | None = None,
                           request_key: str | None = None) -> bool:
        """Persist a returned job without undoing an owner cancellation.

        The paid request may already have been accepted while the owner stopped
        its item. Keep the returned ID for reconciliation, but the cancelled item
        must remain outside the pipeline. A changed request identity or unrelated
        item stage must not receive this older request's result.
        """
        if stage not in {"research", "card"}:
            raise ValueError("unknown submission stage")
        if expected_status == "cancelled":
            raise ValueError("a cancelled item cannot submit a new job")
        job_column = f"{stage}_job"
        request_column = f"{stage}_request_key"
        sets = [f"{job_column}=?", "status=CASE WHEN status='cancelled' THEN status ELSE ? END",
                "error=CASE WHEN status='cancelled' THEN error ELSE NULL END"]
        args = [job, stage]
        if tries is not None:
            sets.append("tries=CASE WHEN status='cancelled' THEN tries ELSE ? END")
            args.append(tries)
        if stage == "card" and expected_status == "research":
            sets.append("research_request_key=CASE WHEN status='cancelled' "
                        "THEN research_request_key ELSE NULL END")
        where = "key=? AND status IN (?,'cancelled')"
        args.extend((key, expected_status))
        if request_key is not None:
            where += f" AND {request_column}=?"
            args.append(request_key)
        with self._c() as c:
            c.execute("BEGIN IMMEDIATE")
            previous = c.execute("SELECT status FROM excel_items WHERE key=?", (key,)).fetchone()
            bound = c.execute(f"UPDATE excel_items SET {','.join(sets)} WHERE {where}", args).rowcount
            return bool(bound and previous and previous[0] == expected_status)

    def retry_failed(self) -> int:
        """Вернуть все failed-позиции в конвейер с чистого листа (status new,
        tries 0, error снят) — команда /excel retry (запрос владельца 2026-07-07:
        «research без фото»/таймауты не терять, а перезапускать одной командой)."""
        with self._c() as c:
            cur = c.execute("UPDATE excel_items SET status='new', tries=0, "
                            "error=NULL, research_job=NULL, card_job=NULL, "
                            "research_request_key=NULL, card_request_key=NULL "
                            "WHERE status='failed'")
            return cur.rowcount

    # ── кэш research (волна 1в): повторные модели не дёргают ChatGPT ──────────
    def cache_get(self, model_key: str):
        with self._c() as c:
            row = c.execute("SELECT utp, photo_path FROM research_cache WHERE model_key=?",
                            (model_key,)).fetchone()
        return row if row else None

    def cache_evidence(self, model_key: str) -> dict | None:
        with self._c() as c:
            row = c.execute("SELECT evidence FROM research_cache WHERE model_key=?",
                            (model_key,)).fetchone()
        if not row or not row[0]:
            return None
        try:
            return json.loads(row[0])
        except (TypeError, ValueError):
            return None

    def cache_put(self, model_key: str, utp: str, photo_path: str | None,
                  source: str = "research", evidence: dict | None = None) -> None:
        with self._c() as c:
            # ручные УТП (source='manual') приоритетнее — research их не перезаписывает
            row = c.execute("SELECT source FROM research_cache WHERE model_key=?",
                            (model_key,)).fetchone()
            if row and row[0] == "manual" and source != "manual":
                return
            c.execute("INSERT OR REPLACE INTO research_cache"
                      "(model_key, utp, photo_path, source, ts, evidence) VALUES(?,?,?,?,?,?)",
                      (model_key, utp, photo_path, source, time.time(),
                       json.dumps(evidence, ensure_ascii=False) if evidence else None))


def _cache_key(item: ExcelItem) -> str:
    return f"{item.brand.strip().lower()}|{item.model.strip().lower()}"


def _category_word(item: ExcelItem) -> str:
    """Слово-категория для research-промпта: часть наименования до бренда
    («Холодильник Beko …» → «Холодильник»)."""
    low = item.name.lower()
    pos = low.find(item.brand.lower())
    head = item.name[:pos].strip() if pos > 0 else ""
    return head or item.name.split()[0]


def parse_research_result(raw: str | None) -> tuple[str, dict | None]:
    try:
        data = json.loads(raw or "")
    except (TypeError, ValueError):
        return raw or "", None
    features = data.get("features") if isinstance(data, dict) else None
    if data.get("exact_model") is not True or not data.get("source_url") or not isinstance(features, list):
        return raw or "", None
    clean = [str(x).strip() for x in features if str(x).strip()]
    return "\n".join(f"✓ {x}" for x in clean), data


def tick(store: ExcelStore, submit_research, read_job, submit_card, preview,
         max_new: int | None = None, new_key_prefix: str | None = None,
         allowed_keys: set[str] | None = None,
         failed_events: list[tuple[str, str, str]] | None = None,
         resolve_photo=None) -> dict:
    """Один проход конвейера. Инъекции:
    submit_research(brand, model, category) -> job_id
    read_job(job_id) -> (status, output_filename, result_specs, error)
    submit_card(brand, model, utp, photo_path) -> job_id
    preview(item, card_output_filename) -> bool
    resolve_photo(item, photo_path) -> usable path or None (optional, no paid I/O)
    """
    stats = {"research": 0, "card": 0, "preview": 0, "failed": 0}

    def _fail_or_retry(item, stage_reset: dict, err: str):
        key_reset = {key: value for key, value in stage_reset.items()
                     if key.endswith("_request_key")}
        if item.tries + 1 >= MAX_TRIES:
            if store.update_if_status(item.key, item.status, status="failed", error=err,
                                      **key_reset):
                stats["failed"] += 1
                if failed_events is not None:
                    failed_events.append((item.key, item.name, err))
        else:
            store.update_if_status(item.key, item.status, tries=item.tries + 1,
                                   error=err, **stage_reset)

    def _record_error(item, operation, exc):
        # A lost response may hide an accepted paid request. Keep its stage and
        # request identity so reconciliation/idempotent submission can recover it.
        error = f"{operation}:{type(exc).__name__}"
        _LOG.warning("Excel operation %s failed: %s", operation, type(exc).__name__)
        try:
            store.update_if_status(item.key, item.status, error=error)
        except sqlite3.Error as db_error:
            _LOG.warning("Excel error persistence failed: %s", type(db_error).__name__)

    def _photo(item, path):
        if not path:
            return None
        if resolve_photo is None:
            return path
        try:
            return resolve_photo(item, path)
        except FileNotFoundError:
            return None

    def _current(item):
        current = store.get(item.key)
        return current is not None and current.status == item.status

    new_items = store.by_status("new")
    if allowed_keys is not None:
        new_items = [item for item in new_items if item.key in allowed_keys]
    if new_key_prefix is not None:
        new_items = [item for item in new_items if item.key.startswith(new_key_prefix)]
    if max_new is not None:
        new_items = new_items[:max(0, max_new)]
    for item in new_items:
        operation = "cache_photo"
        try:
            if not _current(item):
                continue
            cached = store.cache_get(_cache_key(item))
            if cached:
                utp, cached_photo = cached
                evidence = store.cache_evidence(_cache_key(item))
                photo = (_photo(item, cached_photo)
                         if not item.key.startswith("ready-price|") or evidence else None)
                if photo and _current(item):
                    request_key = store.get_or_create_request_key(item.key, "card")
                    operation = "card_submit"
                    try:
                        job = submit_card(item.brand, item.model, utp, photo, item.card_mode,
                                          request_key=request_key)
                    except FileNotFoundError:
                        # The local photo was removed before any request. Fall
                        # through to research; other paid identities stay intact.
                        pass
                    else:
                        if store.bind_submitted_job(item.key, "card", job, "new", tries=item.tries,
                                                    request_key=request_key):
                            stats["card"] += 1
                        continue
            if not _current(item):
                continue
            operation = "research_submit"
            request_key = store.get_or_create_request_key(item.key, "research")
            job = submit_research(item.brand, item.model, _category_word(item),
                                  request_key=request_key)
            if store.bind_submitted_job(item.key, "research", job, "new", request_key=request_key):
                stats["research"] += 1
        except AssertionError:
            raise
        except Exception as exc:
            _record_error(item, operation, exc)

    for item in store.by_status("research"):
        if allowed_keys is not None and item.key not in allowed_keys:
            continue
        operation = "research_read"
        try:
            status, out, utp, err = read_job(item.research_job)
            if not _current(item):
                continue
            if status == "done":
                card_text, evidence = parse_research_result(utp)
                if item.key.startswith("ready-price|") and not evidence:
                    _fail_or_retry(item, {"status": "new", "research_job": None,
                                          "research_request_key": None},
                                   "research без проверяемого источника точной модели")
                    continue
                operation = "research_photo"
                photo = _photo(item, out)
                store.cache_put(_cache_key(item), card_text, photo, evidence=evidence)
                if not photo:
                    _fail_or_retry(item, {"status": "new", "research_job": None,
                                          "research_request_key": None},
                                   "research без доступного фото")
                    continue
                cached = store.cache_get(_cache_key(item))
                if cached is not None:
                    card_text = cached[0]  # Owner-provided UTP has priority.
                if not _current(item):
                    continue
                operation = "card_submit"
                request_key = store.get_or_create_request_key(item.key, "card")
                try:
                    job = submit_card(item.brand, item.model, card_text, photo, item.card_mode,
                                      request_key=request_key)
                except FileNotFoundError:
                    _fail_or_retry(item, {"status": "new", "research_job": None,
                                          "research_request_key": None},
                                   "research без доступного фото")
                    continue
                if store.bind_submitted_job(item.key, "card", job, "research", tries=0,
                                            request_key=request_key):
                    stats["card"] += 1
            elif status == "failed":
                _fail_or_retry(item, {"status": "new", "research_job": None,
                                      "research_request_key": None},
                               f"research: {err or 'ошибка'}")
        except AssertionError:
            raise
        except Exception as exc:
            _record_error(item, operation, exc)

    for item in store.by_status("card"):
        if allowed_keys is not None and item.key not in allowed_keys:
            continue
        operation = "card_read"
        try:
            status, out, _, err = read_job(item.card_job)
            if not _current(item):
                continue
            if status == "done" and out:
                operation = "preview"
                result = preview(item, out)
                if isinstance(result, tuple):
                    accepted, preview_error = result
                else:
                    accepted, preview_error = bool(result), None
                if accepted:
                    if store.update_if_status(item.key, "card", status="preview",
                                              card_request_key=None, error=None):
                        stats["preview"] += 1
                elif preview_error:
                    _fail_or_retry(item, {"status": "new", "card_job": None,
                                          "card_request_key": None},
                                   f"card audit: {preview_error}")
            elif status == "failed":
                _fail_or_retry(item, {"status": "new", "card_job": None,
                                      "card_request_key": None},
                               f"card: {err or 'ошибка'}")
        except AssertionError:
            raise
        except Exception as exc:
            _record_error(item, operation, exc)
    return stats
