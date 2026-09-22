"""Отдельная безопасная очередь контента для готового прайса Никиты."""
from __future__ import annotations

from collections import Counter
import json
import re
import sqlite3
import time
from pathlib import Path

from content_factory.ingest.excel_price import extract_model
from content_factory.orchestrator.excel_pipeline import ExcelStore

SOURCE = "telegram-nikita"
TARGET_BUCKETS = {"small", "large", "tv"}


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
    """Синхронизировать только 531 целевую строку, не трогая чужую очередь."""
    store = ExcelStore(state_db)
    with sqlite3.connect(catalog_db) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT c.article,c.sha256,c.data,c.present,b.ad_id "
                          "FROM catalog c LEFT JOIN source_bindings b "
                          "ON b.source=c.source AND b.article=c.article "
                          "WHERE c.source=?", (SOURCE,)).fetchall()
    candidates = []
    missing = 0
    for row in rows:
        data = json.loads(row["data"])
        if data.get("bucket") not in TARGET_BUCKETS:
            continue
        if not row["present"]:
            missing += 1
            continue
        candidates.append((row, data))
    identities = Counter(_identity(data) for _, data in candidates)
    queued = managed = duplicate = no_model = 0
    status_rows = []
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
    return {"target": len(candidates) + missing, "queued": queued,
            "managed_existing": managed, "held_duplicate_identity": duplicate,
            "held_missing_model": no_model, "missing": missing}


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
