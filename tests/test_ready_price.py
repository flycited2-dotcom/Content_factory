import json
import sqlite3
import time
from datetime import datetime, timezone

from content_factory.ready_price import (
    _model, _outside_product_scope, _retry_delay, batch_keys, batch_status,
    batch_publication_report, cancel_batch, category_counts, control_command,
    next_items, restart_batch,
    set_enabled, start_batch,
    sync_catalog,
)
from content_factory.orchestrator.excel_pipeline import ExcelStore
from content_factory.orchestrator.excel_run import reuse_ready_price_archive


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


def test_owner_stop_controls_publication_and_failed_restart_keeps_stop(tmp_path, monkeypatch):
    state = tmp_path / "state.db"
    ExcelStore(state).add_items([("ready-price|A-1", "BQ", "M1", "Товар", 1000)])
    from content_factory.orchestrator.generation import set_generation_enabled
    set_generation_enabled(state, True)
    path = tmp_path / "publication-control.json"
    monkeypatch.setenv("READY_PRICE_PUBLICATION_CONTROL", str(path))
    assert "Передача новых карточек в Avito остановлена" in control_command("pause", state)
    assert json.loads(path.read_text())["enabled"] is False
    assert control_command("restart", state).startswith("❌")
    assert json.loads(path.read_text())["enabled"] is False
    assert not control_command("start 1", state).startswith("❌")
    assert json.loads(path.read_text())["enabled"] is True
    control_command("cancel", state)
    assert json.loads(path.read_text())["enabled"] is False


def test_broken_publication_control_does_not_allow_generation(tmp_path, monkeypatch):
    state = tmp_path / "state.db"
    ExcelStore(state).add_items([("ready-price|A-1", "BQ", "M1", "Товар", 1000)])
    from content_factory.orchestrator.generation import set_generation_enabled
    set_generation_enabled(state, True)
    parent = tmp_path / "not-directory"
    parent.write_text("file")
    monkeypatch.setenv("READY_PRICE_PUBLICATION_CONTROL", str(parent / "control.json"))
    assert control_command("start 1", state).startswith("❌")
    assert not batch_status(state)["enabled"]


def test_factory_cancelled_backlog_can_start_only_a_selected_finite_batch(tmp_path):
    rows = [(f"A-{i}", _item(f"A-{i}", f"Чайник BQ KT{i}"), 1) for i in range(1, 6)]
    catalog = _catalog(tmp_path / "catalog.db", rows)
    state = tmp_path / "state.db"
    sync_catalog(catalog, state)
    store = ExcelStore(state)
    for i in range(1, 6):
        store.update(f"ready-price|A-{i}", status="cancelled")
    store.update("ready-price|A-5", research_job=99)
    assert sum(n for _, n in category_counts(state, catalog)) == 4
    assert start_batch(state, 2, catalog_db=catalog)["selected"] == 2
    assert sum(store.get(f"ready-price|A-{i}").status == "new" for i in range(1, 6)) == 2
    assert store.get("ready-price|A-5").research_job == 99


def test_master_stop_cannot_be_bypassed_by_avito_start(tmp_path, monkeypatch):
    state = tmp_path / "state.db"
    ExcelStore(state).add_items([("ready-price|A-1", "BQ", "M1", "Товар", 1000)])
    path = tmp_path / "publication-control.json"
    path.write_text('{"enabled":false}')
    monkeypatch.setenv("READY_PRICE_PUBLICATION_CONTROL", str(path))
    assert "Общая генерация выключена" in control_command("start 1", state)
    assert json.loads(path.read_text())["enabled"] is False
    assert not batch_status(state)["batch_id"]


def test_finished_batch_report_explains_publication_hold(tmp_path):
    catalog = _catalog(tmp_path / "catalog.db", [
        ("A-1", _item("A-1", "Аэрогриль BQ GR2001"), 1),
    ])
    state = tmp_path / "state.db"
    sync_catalog(catalog, state)
    start_batch(state, 1, catalog_db=catalog)
    ExcelStore(state).update("ready-price|A-1", status="preview")
    report = tmp_path / "last-run.json"
    report.write_text(json.dumps({"content": {"held": {
        "A-1": "independent_visual_product_audit_required"}}}), encoding="utf-8")
    detail = batch_publication_report(state, catalog, report)
    assert "Аэрогриль BQ GR2001" in detail
    assert "ждёт независимой проверки фото" in detail
    assert "card.png" in detail
    assert "генерация партии завершена" in control_command("status", state)


def test_exact_article_archive_skips_generation_and_refreshes_price(tmp_path):
    from PIL import Image
    catalog = _catalog(tmp_path / "catalog.db", [
        ("A-1", _item("A-1", "Чайник BQ KT100", price=1200), 1)])
    state = tmp_path / "state.db"
    sync_catalog(catalog, state)
    item = ExcelStore(state).get("ready-price|A-1")
    folder = tmp_path / "content" / "A-1"
    folder.mkdir(parents=True)
    for name in ("card.png", "original.png"):
        Image.new("RGB", (128, 128), "blue").save(folder / name, compress_level=0)
    manifest = {"schema_version": 1, "article": "A-1", "brand": item.brand,
                "model": item.model, "name": item.name, "card_mode": item.card_mode,
                "price": 1000, "card": "card.png", "original": "original.png",
                "card_text_audit": {"passed": True}, "evidence": {"exact_model": True}}
    (folder / "content.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert reuse_ready_price_archive(ExcelStore(state), state, tmp_path / "content") == {
        "reused": 1, "prices_refreshed": 1}
    assert ExcelStore(state).get("ready-price|A-1").status == "preview"
    assert json.loads((folder / "content.json").read_text())["price"] == 1200
    assert reuse_ready_price_archive(ExcelStore(state), state, tmp_path / "content") == {
        "reused": 0, "prices_refreshed": 0}


def test_report_keeps_per_article_rejection_when_publisher_is_blocked(tmp_path):
    catalog = _catalog(tmp_path / "catalog.db", [
        ("A-1", _item("A-1", "Аэрогриль BQ GR2005"), 1),
        ("A-2", _item("A-2", "Аэрогриль BQ GR2001"), 1)],
        bindings=(("A-1", "nikita-A-1"), ("A-2", "nikita-A-2")))
    state = tmp_path / "state.db"
    store = ExcelStore(state)
    store.add_items([("ready-price|A-1", "BQ", "GR2005", "Аэрогриль BQ GR2005", 1000),
                     ("ready-price|A-2", "BQ", "GR2001", "Аэрогриль BQ GR2001", 1000)])
    start_batch(state, 2)
    for key in ("ready-price|A-1", "ready-price|A-2"):
        store.update(key, status="preview")
    with sqlite3.connect(catalog) as db:
        db.execute("CREATE TABLE content_publications(article TEXT,status TEXT,reason TEXT,ad_id TEXT)")
        db.executemany("INSERT INTO content_publications VALUES(?,?,?,?)", [
            ("A-1", "held", "avito_rejected", "nikita-A-1"),
            ("A-2", "accepted", "avito_report_active", "nikita-A-2")])
        db.execute("CREATE TABLE publication_batches(status TEXT,receipt TEXT)")
        receipt = [{"ad_id": "nikita-A-1", "messages": [{"type": "error", "code": 2204}]}]
        db.execute("INSERT INTO publication_batches VALUES('rejected',?)", (json.dumps(receipt),))
    report = tmp_path / "last-run.json"
    report.write_text(json.dumps({"content": {"status": "blocked_rejected_batch", "held": {},
                                              "rejected": {"error_codes": ["2204"]}}}))
    text = batch_publication_report(state, catalog, report)
    assert "отклонено Avito; код 2204" in text
    assert "активация подтверждена отчётом Avito" in text
    assert "Публикация приостановлена" in text


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


def test_restart_cancelled_batch_reuses_finished_agent_results(tmp_path):
    catalog = _catalog(tmp_path / "catalog.db", [
        ("A-1", _item("A-1", "Чайник BQ KT100"), 1),
        ("A-2", _item("A-2", "Чайник BQ KT200"), 1)])
    state = tmp_path / "state.db"
    sync_catalog(catalog, state)
    start_batch(state, 2, catalog_db=catalog)
    store = ExcelStore(state)
    store.update("ready-price|A-1", status="cancelled", research_job=10, card_job=11)
    store.update("ready-price|A-2", status="cancelled", research_job=20)
    queue = tmp_path / "queue.db"
    with sqlite3.connect(queue) as db:
        db.execute("CREATE TABLE jobs(id INTEGER PRIMARY KEY,status TEXT,output_filename TEXT)")
        db.executemany("INSERT INTO jobs VALUES(?,?,?)", [
            (10, "done", "r1.png"), (11, "done", "card1.png"), (20, "done", "r2.png")])
    result = restart_batch(state, queue)
    assert result["reused_results"] == 2
    assert store.get("ready-price|A-1").status == "card"
    assert store.get("ready-price|A-1").card_job == 11
    assert store.get("ready-price|A-2").status == "research"
    assert store.get("ready-price|A-2").research_job == 20
    from content_factory.orchestrator.excel_pipeline import tick

    def no_generation(*args, **kwargs):
        raise AssertionError("already completed card must not be generated again")

    stats = tick(store, no_generation,
                 lambda job: ("done", "card1.png", None, None), no_generation,
                 lambda item, output: output == "card1.png",
                 allowed_keys={"ready-price|A-1"})
    assert stats["preview"] == 1 and stats["research"] == stats["card"] == 0


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
