"""Отдельная безопасная очередь контента для готового прайса Никиты."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path

from content_factory.ingest.excel_price import extract_model
from content_factory.orchestrator.excel_pipeline import ExcelStore

SOURCE = "telegram-nikita"
TARGET_BUCKETS = {"small", "large", "tv"}


def _source_problem(db) -> str | None:
    """Check the accepted supplier snapshot before spending work on its queue."""
    try:
        row = db.execute("SELECT generated_at,manifest FROM releases WHERE source=? "
                         "AND status='accepted' ORDER BY generated_at DESC LIMIT 1", (SOURCE,)).fetchone()
        if not row:
            return "supplier_snapshot_unverified"
        manifest = json.loads(row[1])
        if (manifest.get("schema_version") != 2 or manifest.get("snapshot_kind") != "full"
                or manifest.get("provenance", {}).get("supplier_fallback") is not False):
            return "supplier_snapshot_unverified"
        stamps = [datetime.fromisoformat(row[0]),
                  datetime.fromisoformat(manifest["provenance"]["supplier"]["modified_at"])]
        now = datetime.now(timezone.utc)
        if any(stamp.tzinfo is None for stamp in stamps):
            return "supplier_snapshot_unverified"
        if min(stamps) < now - timedelta(days=4) or max(stamps) > now + timedelta(minutes=10):
            return "supplier_snapshot_stale"
    except (sqlite3.Error, ValueError, KeyError, TypeError):
        return "supplier_snapshot_unverified"
    return None


def _outside_product_scope(data: dict) -> str | None:
    """Группы поставщика иногда содержат аксессуар рядом с самим прибором."""
    if (data.get("group") == "Измельчители пищевых отходов"
            and any(word in data.get("name", "").casefold()
                    for word in ("кнопк", "аксессуар"))):
        return "accessory_not_household_appliance"
    return None


def _retry_delay(error: str | None) -> int:
    value = (error or "").casefold()
    if any(word in value for word in ("слишком много запросов", "timeout", "404", "пропал",
                                      "photo url is not an image", "card audit:")):
        return 30 * 60
    return 6 * 60 * 60


def _identity(data: dict) -> str:
    value = f"{data.get('brand', '')}|{data.get('name', '')}".casefold()
    return re.sub(r"[^a-zа-я0-9]+", "", value)


def _model(data: dict) -> str:
    name = data.get("name", "")
    brand = data.get("brand", "")
    hint = str(data.get("model_hint") or "").strip()
    # model_hint is normally the clean SKU extracted by the price importer.
    # Extend only obvious split model suffixes ("ILS3" -> "ILS3 61291 B").
    # Reject unit fragments produced by old heuristic extraction.
    if (len(re.sub(r"\W", "", hint)) >= 4
            and re.search(r"[A-Za-zА-Яа-я]", hint)
            and re.search(r"\d", hint)
            and not re.search(r"(?i)(?:кг|вт|об|литр|см)\.?\s*\d", hint)):
        match = re.search(re.escape(hint), name, re.IGNORECASE)
        if match:
            suffix = re.split(r"[,;(<]", name[match.end():], maxsplit=1)[0].strip()
            parts = suffix.split()
            continuation = []
            for part in parts:
                clean_part = part.strip("#-–—·,.")
                if re.fullmatch(r"\d{2,}[A-Za-zА-Яа-я]?|[A-Za-zА-Яа-я]", clean_part):
                    continuation.append(clean_part)
                else:
                    break
            return " ".join([hint, *continuation]).strip()
        return hint
    # В прайсе бренд иногда короче/длиннее написания в наименовании
    # (Hotpoint-Ariston / Hotpoint). Берём хвост после полного бренда или его
    # значимой части и отсекаем скобки с характеристиками.
    candidates = [brand, *(x for x in re.split(r"[\s/_-]+", brand) if len(x) >= 3)]
    for candidate in candidates:
        match = re.search(re.escape(candidate), name, re.IGNORECASE)
        if match:
            tail = re.split(r"[,;(<]", name[match.end():], maxsplit=1)[0].strip(" # -–—·,.")
            if tail:
                return re.sub(r"\s+", " ", tail)
    return (hint or extract_model(name, brand)).strip()


def _mode(bucket: str) -> str:
    return "ready_tv" if bucket == "tv" else "ready_light"


def sync_catalog(catalog_db, state_db) -> dict:
    """Синхронизировать подтверждённое наличие в целевых группах."""
    store = ExcelStore(state_db)
    with sqlite3.connect(catalog_db) as db:
        db.row_factory = sqlite3.Row
        source_problem = _source_problem(db)
        rows = db.execute("SELECT c.article,c.sha256,c.data,c.present,b.ad_id "
                          "FROM catalog c LEFT JOIN source_bindings b "
                          "ON b.source=c.source AND b.article=c.article "
                          "WHERE c.source=?", (SOURCE,)).fetchall()
    candidates = []
    missing = unverified = 0
    held_rows = []
    for row in rows:
        data = json.loads(row["data"])
        if data.get("bucket") not in TARGET_BUCKETS:
            continue
        reason = (source_problem or ("absent_from_latest_snapshot" if not row["present"] else
                  "not_in_fresh_supplier_price" if data.get("availability") != "supplier_price_present" else None))
        if reason:
            missing += int(not row["present"])
            unverified += int(bool(row["present"]))
            key = f"ready-price|{row['article']}"
            existing = store.get(key)
            if existing and existing.status in {"new", "failed", "research", "card"}:
                # Keep job IDs so work already submitted can be reused if stock returns.
                store.update(key, status="held", error=reason)
            held_rows.append((row["article"], row["sha256"], "held", reason, data.get("bucket"),
                              data.get("brand"), _model(data), data.get("name"), int(data["avito_price"])))
            continue
        candidates.append((row, data))
    identities = Counter(_identity(data) for _, data in candidates)
    queued = managed = duplicate = no_model = 0
    status_rows = held_rows
    to_add = []
    for row, data in candidates:
        article = row["article"]
        if row["ad_id"]:
            status, reason = "managed_existing", "existing_avito_id_preserved"
            managed += 1
        elif identities[_identity(data)] > 1:
            status, reason = "held", "duplicate_source_identity"
            duplicate += 1
        elif _outside_product_scope(data):
            status, reason = "held", _outside_product_scope(data)
            key = f"ready-price|{article}"
            if store.get(key) is not None:
                store.update(key, status="held", tries=0, error=reason,
                             research_job=None, card_job=None, due_at=None)
        elif not _model(data):
            status, reason = "held", "model_not_extracted"
            no_model += 1
        else:
            key = f"ready-price|{article}"
            if store.get(key) is None:
                to_add.append((key, data.get("brand", ""), _model(data), data.get("name", ""),
                               int(data["avito_price"]), _mode(data["bucket"])))
                queued += 1
            else:
                desired_mode = _mode(data["bucket"])
                fields = {"price": int(data["avito_price"]),
                          "name": data.get("name", ""),
                          "card_mode": desired_mode}
                existing = store.get(key)
                if (existing and existing.status == "held"
                        and existing.error == "accessory_not_household_appliance"):
                    fields.update(status="new", tries=0, error=None, due_at=None)
                if existing and existing.status == "held" and existing.error in {
                    "absent_from_latest_snapshot", "not_in_fresh_supplier_price",
                    "supplier_snapshot_unverified", "supplier_snapshot_stale",
                }:
                    resume = "card" if existing.card_job else "research" if existing.research_job else "new"
                    fields.update(status=resume, error=None, due_at=None)
                if existing and existing.status in {"new", "failed"}:
                    fields.update(brand=data.get("brand", ""), model=_model(data))
                # Этот источник работает без оператора: временный сбой агента
                # автоматически возвращается в очередь после паузы.
                if existing and existing.status == "failed":
                    fields.update(status="new", tries=0, error=None,
                                  research_job=None, card_job=None,
                                  due_at=time.time() + _retry_delay(existing.error))
                if existing and existing.card_mode != desired_mode:
                    # Смена утверждённого владельцем шаблона требует только
                    # новой карточки: проверенный research-кэш переиспользуется.
                    fields.update(status="new", tries=0, error=None,
                                  research_job=None, card_job=None, due_at=None)
                store.update(key, **fields)
            status, reason = "content_pipeline", "queued_or_existing"
        status_rows.append((article, row["sha256"], status, reason, data.get("bucket"),
                            data.get("brand"), _model(data), data.get("name"),
                            int(data["avito_price"])))
    store.add_items(to_add)
    with sqlite3.connect(state_db) as db:
        db.execute("CREATE TABLE IF NOT EXISTS ready_price_items ("
                   "article TEXT PRIMARY KEY, release_sha TEXT, status TEXT, reason TEXT, "
                   "bucket TEXT, brand TEXT, model TEXT, name TEXT, price INTEGER)")
        db.executemany("INSERT OR REPLACE INTO ready_price_items VALUES(?,?,?,?,?,?,?,?,?)",
                       status_rows)
    return {"target": len(candidates) + missing + unverified, "queued": queued,
            "managed_existing": managed, "held_duplicate_identity": duplicate,
            "held_missing_model": no_model, "missing": missing,
            "held_stock_unverified": unverified, "source_problem": source_problem}


def set_enabled(state_db, enabled: bool) -> None:
    with sqlite3.connect(state_db) as db:
        db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT)")
        db.execute("INSERT OR REPLACE INTO settings VALUES('ready_price_generation_enabled',?)",
                   ("1" if enabled else "0",))


def enabled(state_db) -> bool:
    with sqlite3.connect(state_db) as db:
        db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT)")
        row = db.execute("SELECT value FROM settings WHERE key='ready_price_generation_enabled'").fetchone()
    return bool(row) and row[0] == "1"


def _batch_tables(db) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS ready_price_batches ("
               "id TEXT PRIMARY KEY, created_at REAL NOT NULL, requested INTEGER NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS ready_price_batch_items ("
               "item_key TEXT PRIMARY KEY, batch_id TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT)")


def batch_status(state_db) -> dict:
    with sqlite3.connect(state_db, timeout=30) as db:
        _batch_tables(db)
        setting = db.execute("SELECT value FROM settings WHERE key='ready_price_batch_id'").fetchone()
        batch_id = setting[0] if setting else ""
        category_setting = db.execute(
            "SELECT value FROM settings WHERE key='ready_price_batch_category'").fetchone()
        category = category_setting[0] if category_setting and batch_id else ""
        rows = db.execute(
            "SELECT e.status,COUNT(*) FROM ready_price_batch_items b "
            "JOIN excel_items e ON e.key=b.item_key WHERE b.batch_id=? GROUP BY e.status",
            (batch_id,),
        ).fetchall() if batch_id else []
        counts = dict(rows)
        backlog = db.execute(
            "SELECT COUNT(*) FROM excel_items WHERE key LIKE 'ready-price|%' "
            "AND status='new' AND key NOT IN "
            "(SELECT item_key FROM ready_price_batch_items WHERE batch_id=?)",
            (batch_id,),
        ).fetchone()[0]
        orphan_in_flight = db.execute(
            "SELECT COUNT(*) FROM excel_items WHERE key LIKE 'ready-price|%' "
            "AND status IN ('research','card') AND key NOT IN "
            "(SELECT item_key FROM ready_price_batch_items WHERE batch_id=?)",
            (batch_id,),
        ).fetchone()[0]
    return {"enabled": enabled(state_db), "batch_id": batch_id,
            "category": category,
            "counts": counts, "backlog": backlog, "orphan_in_flight": orphan_in_flight,
            "active": sum(counts.get(s, 0) for s in ("new", "research", "card"))}


def batch_keys(state_db) -> set[str]:
    with sqlite3.connect(state_db, timeout=30) as db:
        _batch_tables(db)
        setting = db.execute("SELECT value FROM settings WHERE key='ready_price_batch_id'").fetchone()
        if not setting:
            return set()
        return {row[0] for row in db.execute(
            "SELECT item_key FROM ready_price_batch_items WHERE batch_id=?", (setting[0],))}


def _catalog_queue_rows(db, catalog_db) -> list[dict]:
    """Read the supplier's actual category for each Avito queue item."""
    if not catalog_db or not Path(catalog_db).is_file():
        raise ValueError("Каталог категорий недоступен; партия не запущена")
    with sqlite3.connect(catalog_db) as source:
        if _source_problem(source):
            return []
    db.execute("ATTACH DATABASE ? AS ready_source", (str(catalog_db),))
    rows = db.execute(
        "SELECT e.key,e.status,e.brand,e.model,e.name,e.ts,e.due_at,r.bucket,c.data "
        "FROM excel_items e JOIN ready_price_items r "
        "ON r.article=substr(e.key,length('ready-price|')+1) "
        "JOIN ready_source.catalog c ON c.source=? AND c.article=r.article "
        "WHERE e.key LIKE 'ready-price|%' AND (e.status IN ('new','research','card') "
        "OR (e.status='cancelled' AND e.error IS NULL AND e.research_job IS NULL AND e.card_job IS NULL)) "
        "AND r.status='content_pipeline' AND c.present=1", (SOURCE,),
    ).fetchall()
    return [dict(key=row[0], status=row[1], brand=row[2], model=row[3],
                 name=row[4], ts=row[5], due_at=row[6], bucket=row[7],
                 category=str(json.loads(row[8]).get("group") or "").strip())
            for row in rows if json.loads(row[8]).get("availability") == "supplier_price_present"]


def _matches_category(row: dict, category: str) -> bool:
    query = category.casefold().replace("ё", "е").strip()
    if query.startswith("="):
        return row["category"].casefold().replace("ё", "е") == query[1:].strip()
    bucket_aliases = {
        "тв": "tv", "tv": "tv", "телевизоры": "tv",
        "мелкая": "small", "мбт": "small", "small": "small",
        "крупная": "large", "кбт": "large", "large": "large",
    }
    if query in bucket_aliases:
        return row["bucket"] == bucket_aliases[query]
    return bool(query) and query in row["category"].casefold().replace("ё", "е")


def category_counts(state_db, catalog_db) -> list[tuple[str, int]]:
    with sqlite3.connect(state_db, timeout=30) as db:
        rows = _catalog_queue_rows(db, catalog_db)
    now = time.time()
    counts = Counter(row["category"] for row in rows if row["category"] and
                     (row["status"] != "new" or row["due_at"] is None or row["due_at"] <= now))
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def next_items(state_db, catalog_db, category: str = "", limit: int = 10) -> list[dict]:
    with sqlite3.connect(state_db, timeout=30) as db:
        rows = _catalog_queue_rows(db, catalog_db)
    now = time.time()
    rows = [row for row in rows if
            (not category or _matches_category(row, category)) and
            (row["status"] != "new" or row["due_at"] is None or row["due_at"] <= now)]
    return sorted(rows, key=lambda row: (row["status"] == "new", row["ts"] or 0,
                                         row["key"]))[:limit]


def start_batch(state_db, count: int, category: str = "", catalog_db=None) -> dict:
    """Reserve one finite batch; current in-flight work counts toward its size."""
    if not 1 <= count <= 50:
        raise ValueError("Размер партии: от 1 до 50 позиций")
    with sqlite3.connect(state_db, timeout=30) as db:
        db.execute("PRAGMA busy_timeout=30000")
        _batch_tables(db)
        scoped = None
        if catalog_db:
            scoped = {row["key"] for row in _catalog_queue_rows(db, catalog_db)
                      if not category or _matches_category(row, category)}
            if category and not scoped:
                raise ValueError(f"Категория «{category}» не найдена в очереди. "
                                 "Список: /avito categories")
        elif category:
            raise ValueError("Каталог категорий недоступен; партия не запущена")
        db.execute("BEGIN IMMEDIATE")
        setting = db.execute("SELECT value FROM settings WHERE key='ready_price_batch_id'").fetchone()
        current = setting[0] if setting else ""
        if current:
            active = db.execute(
                "SELECT COUNT(*) FROM ready_price_batch_items b "
                "JOIN excel_items e ON e.key=b.item_key WHERE b.batch_id=? "
                "AND e.status IN ('new','research','card')", (current,)).fetchone()[0]
            if active:
                raise ValueError(f"Текущая партия ещё в работе: {active} позиций. "
                                 "Используйте /avito resume или дождитесь завершения.")
        # Research/card jobs already submitted before the first batch remain
        # attached to the source. Count them rather than issuing duplicates.
        in_flight = [row[0] for row in db.execute(
            "SELECT key FROM excel_items WHERE key LIKE 'ready-price|%' "
            "AND status IN ('research','card') ORDER BY ts,key")
            if scoped is None or row[0] in scoped]
        if len(in_flight) > count:
            raise ValueError(f"Уже запущено {len(in_flight)} позиций Avito. "
                             f"Размер новой партии должен быть не меньше {len(in_flight)}.")
        slots = count - len(in_flight)
        new = [row[0] for row in db.execute(
            "SELECT key FROM excel_items WHERE key LIKE 'ready-price|%' "
            "AND (status='new' OR (status='cancelled' AND error IS NULL "
            "AND research_job IS NULL AND card_job IS NULL)) AND (due_at IS NULL OR due_at<=?) "
            "ORDER BY ts,key", (time.time(),))
            if scoped is None or row[0] in scoped][:slots]
        selected = in_flight + new
        if not selected:
            return {"selected": 0, "ongoing": 0, "batch_id": ""}
        db.executemany("UPDATE excel_items SET status='new',tries=0,error=NULL,due_at=NULL "
                       "WHERE key=? AND status='cancelled'", [(key,) for key in new])
        batch_id = uuid.uuid4().hex[:12]
        db.execute("INSERT INTO ready_price_batches VALUES (?,?,?)",
                   (batch_id, time.time(), count))
        db.executemany("INSERT OR REPLACE INTO ready_price_batch_items VALUES (?,?)",
                       [(key, batch_id) for key in selected])
        db.execute("INSERT OR REPLACE INTO settings VALUES('ready_price_batch_id',?)",
                   (batch_id,))
        db.execute("INSERT OR REPLACE INTO settings VALUES('ready_price_batch_category',?)",
                   (category,))
        db.execute("INSERT OR REPLACE INTO settings VALUES('ready_price_generation_enabled','1')")
    return {"selected": len(selected), "ongoing": len(in_flight),
            "batch_id": batch_id, "category": category}


def resume_batch(state_db) -> dict:
    status = batch_status(state_db)
    if not status["batch_id"] or not status["active"]:
        raise ValueError("Нет незавершённой партии. Запустите /avito start 10")
    set_enabled(state_db, True)
    return batch_status(state_db)


def cancel_batch(state_db, queue_db=None) -> dict:
    """Stop this Avito batch and detach its unfinished items from the pipeline."""
    set_enabled(state_db, False)
    with sqlite3.connect(state_db, timeout=30) as db:
        _batch_tables(db)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT value FROM settings WHERE key='ready_price_batch_id'").fetchone()
        batch_id = row[0] if row else ""
        if not batch_id:
            return {"cancelled": 0, "queue_cancelled": 0, "processing": 0}
        items = db.execute(
            "SELECT e.key,e.research_job,e.card_job FROM excel_items e "
            "JOIN ready_price_batch_items b ON b.item_key=e.key "
            "WHERE b.batch_id=? AND e.status IN ('new','research','card','failed')",
            (batch_id,),
        ).fetchall()
        job_ids = sorted({int(job_id) for _, research_job, card_job in items
                          for job_id in (research_job, card_job) if job_id is not None})
        queue_cancelled = processing = 0
        if job_ids and (not queue_db or not Path(queue_db).is_file()):
            raise ValueError("Очередь фотоагента недоступна; отмена не выполнена")
        if job_ids:
            with sqlite3.connect(queue_db, timeout=30) as queue:
                queue.execute("BEGIN IMMEDIATE")
                for job_id in job_ids:
                    queue_cancelled += queue.execute(
                        "UPDATE jobs SET status='cancelled',updated_at=CURRENT_TIMESTAMP "
                        "WHERE id=? AND status='pending'", (job_id,),
                    ).rowcount
                    processing += queue.execute(
                        "SELECT COUNT(*) FROM jobs WHERE id=? AND status='processing'",
                        (job_id,),
                    ).fetchone()[0]
        db.executemany("UPDATE excel_items SET status='cancelled',error='avito_batch_cancelled' "
                       "WHERE key=? AND status IN ('new','research','card','failed')",
                       [(key,) for key, _, _ in items])
    return {"cancelled": len(items), "queue_cancelled": queue_cancelled,
            "processing": processing}


def restart_batch(state_db, queue_db=None) -> dict:
    """Retry cancelled/failed members of the current batch from a clean stage."""
    with sqlite3.connect(state_db, timeout=30) as db:
        _batch_tables(db)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT value FROM settings WHERE key='ready_price_batch_id'").fetchone()
        batch_id = row[0] if row else ""
        if not batch_id:
            raise ValueError("Нет партии для перезапуска. Выберите категорию и размер партии")
        items = db.execute(
            "SELECT e.key,e.status,e.research_job,e.card_job FROM excel_items e "
            "JOIN ready_price_batch_items b ON b.item_key=e.key WHERE b.batch_id=?",
            (batch_id,),
        ).fetchall()
        if not items:
            raise ValueError("В текущей партии нет позиций для перезапуска")
        job_ids = sorted({int(job_id) for _, status, research_job, card_job in items
                          if status in ('research', 'card', 'cancelled')
                          for job_id in (research_job, card_job) if job_id is not None})
        if job_ids and (not queue_db or not Path(queue_db).is_file()):
            raise ValueError("Очередь фотоагента недоступна; перезапуск не выполнен")
        completed_jobs = set()
        if job_ids:
            with sqlite3.connect(queue_db, timeout=30) as queue:
                placeholders = ','.join('?' for _ in job_ids)
                active = queue.execute(
                    f"SELECT COUNT(*) FROM jobs WHERE id IN ({placeholders}) "
                    "AND status IN ('pending','processing')", job_ids,
                ).fetchone()[0]
                if active:
                    raise ValueError(f"Фотоагент ещё выполняет {active} задач(и) этой партии. "
                                     "Дождитесь завершения и повторите /avito restart")
                columns = {row[1] for row in queue.execute("PRAGMA table_info(jobs)")}
                if "output_filename" in columns:
                    completed_jobs = {row[0] for row in queue.execute(
                        f"SELECT id FROM jobs WHERE id IN ({placeholders}) "
                        "AND status='done' AND COALESCE(output_filename,'')<>''", job_ids)}
        retry = [key for key, status, _, _ in items if status in ('cancelled', 'failed')]
        resumed = {}
        for key, status, research_job, card_job in items:
            if status != "cancelled":
                continue
            if card_job in completed_jobs:
                resumed[key] = ("card", research_job, card_job)
            elif research_job in completed_jobs:
                resumed[key] = ("research", research_job, None)
        db.executemany(
            "UPDATE excel_items SET status='new',tries=0,error=NULL,research_job=NULL,"
            "card_job=NULL,research_request_key=NULL,card_request_key=NULL,due_at=NULL "
            "WHERE key=?", [(key,) for key in retry if key not in resumed],
        )
        for key, (stage, research_job, card_job) in resumed.items():
            db.execute(
                "UPDATE excel_items SET status=?,tries=0,error=NULL,research_job=?,"
                "card_job=?,research_request_key=NULL,card_request_key=NULL,due_at=NULL "
                "WHERE key=?", (stage, research_job, card_job, key))
        active = sum(status in ('new', 'research', 'card', 'cancelled', 'failed')
                     for _, status, _, _ in items)
        if not active:
            raise ValueError("Все позиции текущей партии уже готовы. Выберите новую партию")
        db.execute("INSERT OR REPLACE INTO settings VALUES('ready_price_generation_enabled','1')")
    return {"restarted": len(retry), "reused_results": len(resumed), "active": active,
            "category": batch_status(state_db)['category']}


def _generation_control_command(arg: str | None, state_db, catalog_db=None, queue_db=None) -> str:
    """Owner-facing control of Avito content only; photoagents stay independent."""
    parts = (arg or "").strip().lower().split()
    if parts and parts[0] in {"pause", "stop", "off", "стоп", "пауза"}:
        set_enabled(state_db, False)
        return ("⏸ Генерация контента для Avito остановлена. Уже взятые фотоагентом "
                "задачи могут завершиться; новые этапы не запускаются. "
                "Фотоагенты и обновление цен работают отдельно. /avito resume")
    if parts and parts[0] in {"cancel", "отмена", "отменить"}:
        try:
            result = cancel_batch(state_db, queue_db)
        except ValueError as exc:
            return f"❌ {exc}"
        return (f"🛑 Партия Avito отменена: {result['cancelled']} незавершённых "
                f"позиций; снято из очереди фотоагента {result['queue_cancelled']}. "
                f"Уже выполняются: {result['processing']} (они завершатся без "
                "продолжения конвейера). Перезапуск этой партии: /avito restart")
    if parts and parts[0] in {"restart", "перезапуск"}:
        try:
            result = restart_batch(state_db, queue_db)
        except ValueError as exc:
            return f"❌ {exc}"
        return (f"🔄 Партия Avito перезапущена: {result['active']} позиций, "
                f"повторно поставлено {result['restarted']}; "
                f"готовых результатов сохранено {result.get('reused_results', 0)}; "
                f"категория {result['category'] or 'общая'}. Остановить: /avito cancel")
    if parts and parts[0] in {"resume", "on", "продолжить"}:
        try:
            status = resume_batch(state_db)
        except ValueError as exc:
            return f"❌ {exc}"
        return (f"▶️ Продолжаю текущую партию Avito: в работе {status['active']}, "
                f"категория {status['category'] or 'общая'}, "
                f"ожидают следующих партий {status['backlog']}. /avito pause")
    if parts and parts[0] in {"categories", "категории"}:
        try:
            categories = category_counts(state_db, catalog_db)
        except ValueError as exc:
            return f"❌ {exc}"
        lines = ["📦 Категории в очереди Avito (доступны к старту):"]
        lines.extend(f"{name} — {count}" for name, count in categories)
        lines.append("Запуск: /avito start 5 телевизоры или /avito start 5 кофемашины")
        return "\n".join(lines)[:3900]
    if parts and parts[0] in {"queue", "очередь"}:
        category = " ".join(parts[1:])
        try:
            rows = next_items(state_db, catalog_db, category)
        except ValueError as exc:
            return f"❌ {exc}"
        if not rows:
            return f"В очереди нет доступных позиций по запросу «{category}»."
        lines = ["Первые позиции Avito" + (f" · {category}" if category else "") + ":"]
        lines.extend(f"{i}. {'🔎' if row['status'] != 'new' else '🆕'} "
                     f"{row['name']} [{row['category']}]"
                     for i, row in enumerate(rows, 1))
        return "\n".join(lines)
    if parts and parts[0] in {"report", "отчёт", "отчет", "где"}:
        return batch_publication_report(state_db, catalog_db)
    if parts and parts[0] in {"start", "next", "партия"}:
        if len(parts) < 2 or not parts[1].isdigit():
            return "❌ Формат: /avito start 10 [категория] (от 1 до 50 позиций)"
        category = " ".join(parts[2:])
        try:
            result = start_batch(state_db, int(parts[1]), category, catalog_db)
        except ValueError as exc:
            return f"❌ {exc}"
        if not result["selected"]:
            return "✅ Новых позиций для генерации Avito сейчас нет."
        return ("▶️ Партия Avito" + (f" · {category}" if category else "") +
                f": {result['selected']} позиций, из них "
                f"{result['ongoing']} уже были в работе. /avito pause — остановить; "
                "/avito — статус.")
    if parts and parts[0] not in {"status", "статус"}:
        return ("❌ Категория ещё не выбрана. Нажмите «Категории», затем товарную "
                "группу и размер партии. Или введите: /avito start 5 телевизоры")
    status = batch_status(state_db)
    mode = ("⏸ пауза" if not status["enabled"] else
            "✅ генерация партии завершена" if status["batch_id"] and not status["active"]
            and status["counts"].get("preview", 0) else
            "▶️ идёт" if status["enabled"] else "⏸ пауза")
    counts = status["counts"]
    category_label = status['category'].lstrip('=') or 'общая'
    return (f"Avito-контент: {mode}. "
            f"Текущая партия ({category_label}): "
            f"🆕 {counts.get('new', 0)} · 🔎 {counts.get('research', 0)} · "
            f"🎨 {counts.get('card', 0)} · ✅ {counts.get('preview', 0)} · "
            f"❌ {counts.get('failed', 0)} · 🛑 отменено {counts.get('cancelled', 0)}. "
            "Уже начаты вне партии: "
            f"{status['orphan_in_flight']}. Следующих позиций: {status['backlog']}.\n"
            "Команды: /avito queue · /avito categories · "
            "/avito start 5 телевизоры · /avito pause · /avito cancel · "
            "/avito restart · /avito report. "
            "Фотоагенты, цены и передача готовых карточек управляются отдельно.")


def control_command(arg: str | None, state_db, catalog_db=None, queue_db=None) -> str:
    """One owner switch stops both generation and new-card publication."""
    raw_path = os.environ.get("READY_PRICE_PUBLICATION_CONTROL", "")
    action = (arg or "status").strip().lower().split()[0] if (arg or "").strip() else "status"
    if action in {"stock", "остатки", "наличие"}:
        path = Path(os.getenv("AVITO_STOCK_AUDIT_REPORT", "/opt/avito-bridge/state/stock-audit/last-run.json"))
        try:
            audit = json.loads(path.read_text(encoding="utf-8"))
            lines = [f"📦 Проверка наличия: {audit['generated_at']}",
                     f"В фиде {audit['after']}. Подтверждены {audit['verified']}. "
                     f"Удержаны {audit['held']}. Ждут связи с поставщиком {audit['deferred']}.",
                     "Подтверждение: точный артикул в свежих остатках или прайсе поставщика."]
            lines.extend(f"• {row['title']} — наличие не подтверждено"
                         for row in audit['rows'] if row['status'] in {'held', 'unverified', 'not_available'})
            lines.append("Удержанные товары не возвращаются автоматически без подтверждения источника. Карточки сохранены.")
            return "\n".join(lines)[:3900]
        except (OSError, ValueError, KeyError, TypeError):
            return "⚠️ Отчёт проверки наличия пока недоступен."
    stopping = action in {"pause", "stop", "off", "стоп", "пауза", "cancel", "отмена", "отменить"}
    starting = action in {"start", "next", "партия", "resume", "on", "продолжить", "restart", "перезапуск"}
    if raw_path and starting:
        from content_factory.orchestrator.generation import generation_enabled
        if not generation_enabled(state_db):
            return ("❌ Общая генерация выключена. Сначала /generation on, затем "
                    "/avito categories → категория → размер партии. "
                    "Включение общей генерации не запускает всю очередь Avito.")

    def write_control(value):
        path = Path(raw_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as stream:
            json.dump({"enabled": value, "reason": "owner_command", "action": action,
                       "updated_at": datetime.now(timezone.utc).isoformat()}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)

    if raw_path and stopping:
        # Persist the publication stop first, even if queue cancellation fails.
        try:
            write_control(False)
        except OSError:
            set_enabled(state_db, False)
            return "❌ Генерация остановлена, но блокировка публикации недоступна. Проверьте сервис Avito."
    reply = _generation_control_command(arg, state_db, catalog_db, queue_db)
    if action in {"status", "статус"} and catalog_db:
        try:
            count = sum(n for _, n in category_counts(state_db, catalog_db))
            raw_count = batch_status(state_db)["backlog"]
            reply = reply.replace(f"Следующих позиций: {raw_count}.",
                                  f"Доступны к выбору из свежего прайса: {count}.")
        except (ValueError, sqlite3.Error):
            reply += "\n⚠️ Каталог для выбора партии недоступен."
    if raw_path and starting and not reply.startswith("❌") and enabled(state_db):
        try:
            write_control(True)
        except OSError:
            set_enabled(state_db, False)
            return "❌ Запуск отменён: управление публикацией недоступно."
    if raw_path:
        reply = reply.replace("Фотоагенты, цены и передача готовых карточек управляются отдельно.",
                              "Стоп Avito останавливает генерацию и передачу новых карточек. "
                              "Фотоагенты и обновление цен работают отдельно.")
        from content_factory.orchestrator.generation import generation_enabled
        if not generation_enabled(state_db):
            reply += "\n⏸ Общая генерация выключена: /generation on — разрешить запуск выбранной партии."
        try:
            value = json.loads(Path(raw_path).read_text(encoding="utf-8"))
            paused = value.get("enabled") is not True
        except FileNotFoundError:
            paused = False
        except (OSError, ValueError, TypeError):
            paused = True
        reply += ("\n⏸ Передача новых карточек в Avito остановлена." if paused else
                  "\n📤 Передача новых карточек разрешена после проверок товара и фото.")
        if Path(raw_path).with_name("content-publication.paused").exists():
            reply += "\n⛔ Дополнительная проверка публикации: новые объявления удерживаются."
    return reply[:3900]


_PUBLICATION_REASONS = {
    "independent_visual_product_audit_required": "ждёт независимой проверки фото",
    "exact_model_evidence_required": "нет подтверждения точной модели",
    "content_price_is_not_latest": "цена в архиве устарела",
    "absent_from_latest_snapshot": "нет в свежем прайсе",
    "not_in_fresh_supplier_price": "наличие не подтверждено",
    "insufficient_content_image_resolution": "слишком низкое разрешение фото",
    "avito_card_must_be_2048x1536": "карточка имеет неверный формат, нужен 2048×1536",
    "visual_product_image_changed": "изображение изменилось после проверки",
    "visual_product_identity_changed": "модель отличается от проверенной",
}


def batch_publication_report(state_db, catalog_db=None, report_path=None) -> str:
    """Show each current-batch item's actual publication gate, not just generation."""
    path = Path(report_path or os.environ.get(
        "READY_PRICE_PUBLICATION_REPORT",
        "/opt/avito-bridge/state/ready-price/last-run.json"))
    try:
        publication = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        publication = {}
    try:
        restore = json.loads((Path(state_db).parent / "avito-drive-restore.json").read_text(
            encoding="utf-8"))
    except (OSError, ValueError):
        restore = {}
    held = (publication.get("content") or {}).get("held") or {}
    added = {str(row.get("article")) for row in
             (publication.get("content") or {}).get("added") or []}
    status = batch_status(state_db)
    if not status["batch_id"]:
        return "Текущей партии Avito нет. Запуск: /avito start 5 категория"
    with sqlite3.connect(state_db) as db:
        rows = db.execute(
            "SELECT e.key,e.name,e.status FROM ready_price_batch_items b "
            "JOIN excel_items e ON e.key=b.item_key WHERE b.batch_id=? ORDER BY e.name",
            (status["batch_id"],),
        ).fetchall()
    bound = set()
    recorded = {}
    rejected_codes = {}
    if catalog_db and Path(catalog_db).is_file():
        with sqlite3.connect(catalog_db) as db:
            bound = {r[0] for r in db.execute(
                "SELECT article FROM source_bindings WHERE source=?", (SOURCE,))}
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "content_publications" in tables:
                recorded = {r[0]: {"status": r[1], "reason": r[2], "ad_id": r[3]}
                            for r in db.execute(
                                "SELECT article,status,reason,ad_id FROM content_publications")}
            if "publication_batches" in tables:
                for (raw,) in db.execute("SELECT receipt FROM publication_batches WHERE status='rejected'"):
                    try:
                        receipts = json.loads(raw)
                        for receipt in receipts if isinstance(receipts, list) else []:
                            codes = [str(message["code"]) for message in receipt.get("messages") or []
                                     if message.get("type") == "error" and message.get("code") is not None]
                            if codes:
                                rejected_codes[str(receipt.get("ad_id"))] = ", ".join(codes)
                    except (ValueError, TypeError, KeyError):
                        pass
    lines = [f"📊 Партия Avito: {status['category'].lstrip('=') or 'общая'} "
             f"({len(rows)} позиций).",
             f"Генерация: {'ещё идёт' if status['active'] else 'завершена или остановлена'}. "
             f"Готовых комплектов: {status['counts'].get('preview', 0)}."]
    publication_state = (publication.get("content") or {}).get("status")
    if publication_state == "waiting_previous_batch":
        lines.append("Публикация ждёт отчёта Avito по предыдущей партии; "
                     "остальные карточки будут проверены после него.")
    elif publication_state == "blocked_rejected_batch":
        codes = ", ".join((publication.get("content") or {}).get("rejected", {}).get("error_codes") or [])
        lines.append(f"Публикация приостановлена: Avito отклонил предыдущую партию"
                     + (f" (код {codes})." if codes else ".")
                     + " Автоматическая проверка отчёта продолжается.")
    elif publication.get("content"):
        lines.append(f"Последняя проверка публикации: новых в XML "
                     f"{len((publication['content'] or {}).get('added') or [])}, "
                     f"удержано {len(held)}.")
    for key, name, item_status in rows:
        article = key.split("|", 1)[-1]
        if item_status != "preview":
            detail = {"new": "ждёт запуска", "research": "поиск УТП/фото",
                      "card": "генерация карточки", "failed": "ошибка генерации",
                      "cancelled": "отменено"}.get(item_status, item_status)
        elif article in held:
            reason = held[article]
            detail = _PUBLICATION_REASONS.get(
                reason, reason.replace("required_avito_field_unverified:",
                                       "не подтверждено поле Avito: "))
        elif recorded.get(article, {}).get("reason") == "avito_rejected":
            code = rejected_codes.get(recorded[article]["ad_id"], "")
            detail = "отклонено Avito" + (f"; код {code}" if code else "")
        elif recorded.get(article, {}).get("status") == "held":
            reason = recorded[article]["reason"]
            detail = _PUBLICATION_REASONS.get(reason, reason)
        elif recorded.get(article, {}).get("status") == "accepted":
            detail = "активация подтверждена отчётом Avito"
        elif article in bound:
            detail = "передано в фид Avito; активация — по отчёту Avito"
        elif article in added:
            detail = "добавлено в XML; ожидает отчёт Avito"
        elif publication_state == "waiting_previous_batch":
            detail = "проверка публикации отложена до отчёта Avito"
        else:
            detail = "файл готов; причина публикации ещё не получена"
        if key in (restore.get("deferred") or []):
            detail = ("архив не удалось проверить; повторная генерация отложена"
                      if restore.get("lookup_ok") else
                      "ждёт восстановления связи с архивом Drive")
        lines.append(f"• {article} · {name[:62]} — {detail}")
    lines.append("Файлы: /opt/avito-ready-price/content/<артикул>/ "
                 "(card.png, original.png, content.json).")
    try:
        drive = json.loads((Path(state_db).parent / "avito-drive-sync.json").read_text(
            encoding="utf-8"))
        age = max(0, time.time() - float(drive["finished_at"]))
        if age < 7200:
            lines.append(f"Google Drive: архив обновлён, {drive['articles']} артикулов. "
                         f"{drive['url']}")
        else:
            lines.append(f"Google Drive: синхронизация отстаёт на {int(age // 3600)} ч. "
                         f"Последний архив: {drive['url']}")
    except (OSError, ValueError, KeyError, TypeError):
        lines.append("Google Drive: первая синхронизация ещё не подтверждена.")
    try:
        restore = json.loads((Path(state_db).parent / "avito-drive-restore.json").read_text(
            encoding="utf-8"))
        if not restore.get("lookup_ok"):
            lines.append("Восстановление архива: Drive временно недоступен; новые задачи "
                         "отложены, чтобы не тратить генерацию повторно.")
        elif restore.get("deferred"):
            lines.append(f"Восстановление архива: отложено "
                         f"{len(restore['deferred'])} позиций; проверка продолжится автоматически.")
        else:
            lines.append(f"Восстановление архива: проверено; возвращено с Drive "
                         f"{restore.get('restored', 0)} карточек за последний проход.")
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return "\n".join(lines)[:3900]
