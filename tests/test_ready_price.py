import json
import sqlite3
import time

from content_factory.ready_price import _model, _outside_product_scope, _retry_delay, sync_catalog


def _catalog(path, rows, bindings=()):
    with sqlite3.connect(path) as db:
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
            "bucket": bucket, "avito_price": price, "source_price": "1000"}


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
                      "missing": 0}
    with sqlite3.connect(tmp_path / "state.db") as db:
        assert db.execute("SELECT key,card_mode FROM excel_items").fetchall() == [
            ("ready-price|A-3", "ready_light")]


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
