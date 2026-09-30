import json
import sqlite3
import time
from datetime import datetime, timezone

from content_factory.ready_price import (
    _model, _outside_product_scope, _retry_delay, batch_keys, batch_status,
    cancel_batch, category_counts, control_command, next_items, restart_batch,
    set_enabled, start_batch,
    sync_catalog,
)
from content_factory.orchestrator.excel_pipeline import ExcelStore


def _catalog(path, rows, bindings=()):
    with sqlite3.connect(path) as db:
        stamp = datetime.now(timezone.utc).isoformat()
        db.execute("CREATE TABLE releases(source TEXT,status TEXT,generated_at TEXT,manifest TEXT)")
        db.execute("INSERT INTO releases VALUES(?,?,?,?)", ("telegram-nikita", "accepted", stamp,
                   json.dumps({"schema_version": 2, "snapshot_kind": "full", "provenance": {
                       "supplier_fallback": False, "supplier": {"modified_at": stamp}}})))
        db.execute("CREATE TABLE catalog(source TEXT,article TEXT,sha256 TEXT,data TEXT,present INTEGER,missing_since TEXT,PRIMARY KEY(source,article))")
        db.execute("CREATE TABLE source_bindings(source TEXT,article TEXT,ad_id TEXT,PRIMARY KEY(source,article))")
        for article, data, present in rows:
            db.execute("INSERT INTO catalog VALUES(?,?,?,?,?,NULL)",
                       ("telegram-nikita", article, "release-sha", json.dumps(data), present))
        for article, ad_id in bindings:
            db.execute("INSERT INTO source_bindings VALUES(?,?,?)",
                       ("telegram-nikita", article, ad_id))
    return path


def _item(article, name, bucket="small", price=1050):
    return {"article": article, "brand": "BQ", "name": name, "group": "Телевизоры",
            "bucket": bucket, "avito_price": price, "source_price": "1000",
            "availability": "supplier_price_present"}


def test_avito_content_batches_pause_without_stopping_other_work(tmp_path):
    state = tmp_path / "state.db"
    store = ExcelStore(state)
    store.add_items([(f"ready-price|A-{i}", "BQ", f"M{i}", f"Товар {i}", 1000)
                     for i in range(1, 4)] +
                    [("manual|B-1", "BQ", "X1", "Ручной товар", 1000)])
    store.update("ready-price|A-1", status="research", research_job=42)
    set_enabled(state, False)
    result = start_batch(state, 2)
    assert result["selected"] == 2 and result["ongoing"] == 1
    assert batch_keys(state) == {"ready-price|A-1", "ready-price|A-2"}
    assert "остановлена" in control_command("pause", state)
    assert not batch_status(state)["enabled"]
    assert store.get("manual|B-1").status == "new"
    assert "Продолжаю" in control_command("resume", state)
    store.update("ready-price|A-1", status="preview")
    store.update("ready-price|A-2", status="preview")
    assert start_batch(state, 1)["selected"] == 1
    assert batch_keys(state) == {"ready-price|A-3"}


def test_avito_category_batch_skips_inflight_from_other_categories(tmp_path):
    rows = [
        ("TV-1", {**_item("TV-1", "Телевизор BQ TV100", "tv"),
                  "group": "Телевизоры 46 и более"}, 1),
        ("TV-2", {**_item("TV-2", "Телевизор BQ TV200", "tv"),
                  "group": "Телевизоры 27-32"}, 1),
        ("KT-1", {**_item("KT-1", "Чайник BQ KT100", "small"),
                  "group": "Электрочайники"}, 1),
    ]
    catalog = _catalog(tmp_path / "catalog.db", rows)
    state = tmp_path / "state.db"
    sync_catalog(catalog, state)
    store = ExcelStore(state)
    store.update("ready-price|KT-1", status="research", research_job=42)

    assert category_counts(state, catalog) == [
        ("Телевизоры 27-32", 1), ("Телевизоры 46 и более", 1),
        ("Электрочайники", 1)]
    assert [row["key"] for row in next_items(state, catalog, "телевизоры")] == [
        "ready-price|TV-1", "ready-price|TV-2"]
    result = start_batch(state, 2, "телевизоры", catalog)
    assert result["selected"] == 2 and result["ongoing"] == 0
    assert batch_keys(state) == {"ready-price|TV-1", "ready-price|TV-2"}
    assert batch_status(state)["category"] == "телевизоры"
    assert batch_status(state)["orphan_in_flight"] == 1
    assert "Электрочайники — 1" in control_command("categories", state, catalog)
    assert "Телевизор BQ TV100" in control_command("queue телевизоры", state, catalog)
    assert "Категория ещё не выбрана" in control_command(
        "Телевизоры 27-32 — 1", state, catalog)


def test_exact_avito_category_does_not_take_similarly_named_group(tmp_path):
    catalog = _catalog(tmp_path / "catalog.db", [
        ("H-1", {**_item("H-1", "Вытяжка BQ H1", "large"),
                 "group": "Вытяжки"}, 1),
        ("H-2", {**_item("H-2", "Вытяжка BQ H2", "large"),
                 "group": "Вытяжки встраиваемые"}, 1),
    ])
    state = tmp_path / "state.db"
    sync_catalog(catalog, state)
    assert start_batch(state, 1, "=Вытяжки", catalog)["selected"] == 1
    assert batch_keys(state) == {"ready-price|H-1"}


def test_unknown_avito_category_never_starts_unfiltered_batch(tmp_path):
    catalog = _catalog(tmp_path / "catalog.db", [
        ("A-1", {**_item("A-1", "Чайник BQ KT100"),
                  "group": "Электрочайники"}, 1),
    ])
    state = tmp_path / "state.db"
    sync_catalog(catalog, state)
    assert "не найдена" in control_command("start 5 телевизоры", state, catalog)
    assert not batch_status(state)["enabled"]
    assert batch_keys(state) == set()


def test_cancel_avito_batch_stops_only_its_pending_jobs_and_restart_is_safe(tmp_path):
    state = tmp_path / "state.db"
    queue = tmp_path / "queue.db"
    store = ExcelStore(state)
    store.add_items([(f"ready-price|A-{i}", "BQ", f"M{i}", f"Товар {i}", 1000)
                     for i in range(1, 4)] +
                    [("manual|B-1", "BQ", "X1", "Ручной товар", 1000)])
    start_batch(state, 3)
    store.update("ready-price|A-1", status="research", research_job=10)
    store.update("ready-price|A-2", status="research", research_job=11)
    store.update("manual|B-1", status="research", research_job=12)
    with sqlite3.connect(queue) as db:
        db.execute("CREATE TABLE jobs(id INTEGER PRIMARY KEY,status TEXT,updated_at TEXT)")
        db.executemany("INSERT INTO jobs(id,status) VALUES(?,?)",
                       [(10, "pending"), (11, "processing"), (12, "pending")])

    result = cancel_batch(state, queue)
    assert result == {"cancelled": 3, "queue_cancelled": 1, "processing": 1}
    assert not batch_status(state)["enabled"]
    assert batch_status(state)["active"] == 0
    assert "🛑 отменено 3" in control_command(None, state)
    assert [store.get(f"ready-price|A-{i}").status for i in range(1, 4)] == [
        "cancelled", "cancelled", "cancelled"]
    assert store.get("manual|B-1").status == "research"
    with sqlite3.connect(queue) as db:
        assert db.execute("SELECT id,status FROM jobs ORDER BY id").fetchall() == [
            (10, "cancelled"), (11, "processing"), (12, "pending")]
    try:
        restart_batch(state, queue)
    except ValueError as exc:
        assert "ещё выполняет" in str(exc)
    else:
        assert False, "restart must wait for in-flight agent job"
    with sqlite3.connect(queue) as db:
        db.execute("UPDATE jobs SET status='done' WHERE id=11")
    restarted = restart_batch(state, queue)
    assert restarted["restarted"] == 3 and restarted["active"] == 3
    assert batch_status(state)["enabled"]
    assert store.get("ready-price|A-1").research_job is None
    assert store.get("ready-price|A-1").status == "new"


def test_sync_queues_unique_targets_and_holds_duplicate_identity(tmp_path):
    catalog = _catalog(tmp_path / "catalog.db", [
        ("A-1", _item("A-1", "Телевизор BQ 43F34B", "tv"), 1),
        ("A-2", _item("A-2", "Телевизор BQ 43F34B", "tv"), 1),
        ("A-3", _item("A-3", "Чайник BQ KT100", "small"), 1),
        ("A-4", _item("A-4", "Посуда BQ PAN", "excluded"), 1),
    ])
    result = sync_catalog(catalog, tmp_path / "state.db")
    assert result == {"target": 3, "queued": 1, "managed_existing": 0,
                      "held_duplicate_identity": 2, "held_missing_model": 0,
                      "missing": 0, "held_stock_unverified": 0, "source_problem": None}
    with sqlite3.connect(tmp_path / "state.db") as db:
        assert db.execute("SELECT key,card_mode FROM excel_items").fetchall() == [
            ("ready-price|A-3", "ready_light")]


def test_stock_absence_holds_inflight_job_and_return_reuses_it(tmp_path):
    catalog = _catalog(tmp_path / "catalog.db", [("A-1", _item("A-1", "Чайник BQ KT100"), 1)])
    state = tmp_path / "state.db"
    sync_catalog(catalog, state)
    store = ExcelStore(state)
    store.update("ready-price|A-1", status="card", card_job=77)
    with sqlite3.connect(catalog) as db:
        data = _item("A-1", "Чайник BQ KT100")
        data["availability"] = "unverified_origin"
        db.execute("UPDATE catalog SET data=?", (json.dumps(data),))
    assert sync_catalog(catalog, state)["held_stock_unverified"] == 1
    assert store.get("ready-price|A-1").status == "held"
    assert next_items(state, catalog) == []
    assert start_batch(state, 1, catalog_db=catalog)["selected"] == 0
    with sqlite3.connect(catalog) as db:
        data["availability"] = "supplier_price_present"
        db.execute("UPDATE catalog SET data=?", (json.dumps(data),))
    sync_catalog(catalog, state)
    assert store.get("ready-price|A-1").status == "card"
    assert store.get("ready-price|A-1").card_job == 77


def test_stale_supplier_stops_generation_and_category_selection(tmp_path):
    catalog = _catalog(tmp_path / "catalog.db", [("A-1", _item("A-1", "Чайник BQ KT100"), 1)])
    state = tmp_path / "state.db"
    sync_catalog(catalog, state)
    with sqlite3.connect(catalog) as db:
        db.execute("UPDATE releases SET generated_at='2020-01-01T00:00:00+00:00'")
    assert sync_catalog(catalog, state)["source_problem"] == "supplier_snapshot_stale"
    assert ExcelStore(state).get("ready-price|A-1").status == "held"
    assert category_counts(state, catalog) == []


def test_sync_updates_price_without_reset_and_skips_bound_item(tmp_path):
    catalog = _catalog(tmp_path / "catalog.db", [
        ("A-1", _item("A-1", "Телевизор BQ 43F34B", "tv"), 1),
        ("A-2", _item("A-2", "Чайник BQ KT100", "small"), 1),
    ], bindings=(("A-1", "historical-id"),))
    state = tmp_path / "state.db"
    first = sync_catalog(catalog, state)
    assert first["managed_existing"] == 1 and first["queued"] == 1
    with sqlite3.connect(state) as db:
        db.execute("UPDATE excel_items SET status='card',card_job=77 WHERE key='ready-price|A-2'")
    with sqlite3.connect(catalog) as db:
        data = json.loads(db.execute("SELECT data FROM catalog WHERE article='A-2'").fetchone()[0])
        data["avito_price"] = 1200
        db.execute("UPDATE catalog SET data=? WHERE article='A-2'", (json.dumps(data),))
    second = sync_catalog(catalog, state)
    assert second["queued"] == 0
    with sqlite3.connect(state) as db:
        assert db.execute("SELECT price,status,card_job FROM excel_items WHERE key='ready-price|A-2'").fetchone() == (1200, "card", 77)


def test_sync_reschedules_failed_ready_price_item_without_operator(tmp_path):
    catalog = _catalog(tmp_path / "catalog.db", [
        ("A-1", _item("A-1", "Чайник BQ KT100", "small"), 1),
    ])
    state = tmp_path / "state.db"
    sync_catalog(catalog, state)
    with sqlite3.connect(state) as db:
        db.execute("UPDATE excel_items SET status='failed',tries=2,error='Timeout 20000ms',"
                   "research_job=10,card_job=11 WHERE key='ready-price|A-1'")
    before = time.time()
    sync_catalog(catalog, state)
    with sqlite3.connect(state) as db:
        row = db.execute("SELECT status,tries,error,research_job,card_job,due_at "
                         "FROM excel_items WHERE key='ready-price|A-1'").fetchone()
    assert row[:5] == ("new", 0, None, None, None)
    assert before + 29 * 60 < row[5] < before + 31 * 60


def test_sync_stops_previously_queued_accessory(tmp_path):
    catalog = _catalog(tmp_path / "catalog.db", [
        ("A-1", _item("A-1", "Чайник BQ KT100", "small"), 1),
    ])
    state = tmp_path / "state.db"
    sync_catalog(catalog, state)
    with sqlite3.connect(catalog) as db:
        data = json.loads(db.execute("SELECT data FROM catalog WHERE article='A-1'").fetchone()[0])
        data.update(group="Измельчители пищевых отходов",
                    name="Кнопка для измельчителя NORTEL MBL100")
        db.execute("UPDATE catalog SET data=? WHERE article='A-1'", (json.dumps(data),))
    sync_catalog(catalog, state)
    with sqlite3.connect(state) as db:
        row = db.execute("SELECT status,error FROM excel_items WHERE key='ready-price|A-1'").fetchone()
    assert row == ("held", "accessory_not_household_appliance")


def test_model_uses_name_tail_when_brand_spelling_differs():
    assert _model({"brand": "Hotpoint-Ariston",
                   "name": "Электрическая духовка Hotpoint HSTF 1231 JSAH BLG",
                   "model_hint": ""}) == "HSTF 1231 JSAH BLG"
    assert _model({"brand": "Indesit",
                   "name": "Стиральная машина Indesit ILS3 61291 (6 кг)",
                   "model_hint": "ILS3"}) == "ILS3 61291"


def test_model_prefers_clean_hint_and_drops_description():
    assert _model({"brand": "Maunfeld",
                   "name": "Индукционная панель Maunfeld CVI453SBWH Inverter, 3 конфорки",
                   "model_hint": "CVI453SBWH"}) == "CVI453SBWH"
    assert _model({"brand": "Sakura",
                   "name": "Микроволновая печь Sakura SA-7055W белый, 20 л",
                   "model_hint": "SA-7055W"}) == "SA-7055W"


def test_model_rejects_unit_fragment_hint():
    assert _model({"brand": "HOMELINE",
                   "name": "Стиральная машина HOMELINE WMCI 8120 # (Инвертор 8 кг.1200 об)",
                   "model_hint": "кг.1200"}) == "WMCI 8120"


def test_transient_ready_price_failures_retry_sooner_than_research_miss():
    assert _retry_delay("Page.wait_for_selector: Timeout 20000ms exceeded") == 30 * 60
    assert _retry_delay("Слишком много запросов") == 30 * 60
    assert _retry_delay("research: photo URL is not an image") == 30 * 60
    assert _retry_delay("card audit: unverified_card_text:качество") == 30 * 60
    assert _retry_delay("research: no verified exact-model evidence") == 6 * 60 * 60


def test_disposer_button_is_not_treated_as_a_household_appliance():
    assert _outside_product_scope({"group": "Измельчители пищевых отходов",
                                   "name": "Кнопка для измельчителя NORTEL mbl"})
