import sqlite3
from types import SimpleNamespace

from content_factory.orchestrator.generation import (
    generation_command,
    generation_enabled,
    set_generation_enabled,
)


def _queue_db(path):
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, input_filename TEXT, status TEXT)")
        c.executemany(
            "INSERT INTO jobs(id,input_filename,status) VALUES(?,?,?)",
            [(1, "catalog-a.jpg", "pending"),
             (2, "excel-a.jpg", "pending"),
             (3, "manual.jpg", "pending"),
             (4, "catalog-b.jpg", "processing")],
        )
    return path


def _card_jobs_db(path):
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE card_jobs (key TEXT PRIMARY KEY, input_filename TEXT, "
                  "status TEXT, tries INTEGER DEFAULT 0)")
        c.executemany(
            "INSERT INTO card_jobs(key,input_filename,status) VALUES(?,?,?)",
            [("catalog-a", "catalog-a.jpg", "pending"),
             ("catalog-b", "catalog-b.jpg", "pending"),
             ("catalog-done", "done.jpg", "done")],
        )
    return path


def _state_db(path):
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE excel_items (key TEXT PRIMARY KEY, status TEXT, "
                  "research_job INTEGER, card_job INTEGER)")
        c.executemany(
            "INSERT INTO excel_items VALUES(?,?,?,?)",
            [("excel-a", "card", None, 2),
             ("excel-new", "new", None, None),
             ("excel-done", "preview", None, None)],
        )
    return path


def test_generation_defaults_to_off_and_persists(tmp_path):
    db = tmp_path / "state.db"
    assert generation_enabled(db) is False
    set_generation_enabled(db, True)
    assert generation_enabled(db) is True
    set_generation_enabled(db, False)
    assert generation_enabled(db) is False


def test_generation_off_cancels_only_factory_pending_tail(tmp_path):
    state = _state_db(tmp_path / "state.db")
    cards = _card_jobs_db(tmp_path / "card_jobs.db")
    queue = _queue_db(tmp_path / "queue.db")
    set_generation_enabled(state, True)

    reply = generation_command("off", state, cards, queue)

    assert generation_enabled(state) is False
    assert "2" in reply  # catalog-a + excel-a сняты из общей очереди
    with sqlite3.connect(queue) as c:
        statuses = dict(c.execute("SELECT id,status FROM jobs"))
    assert statuses == {1: "cancelled", 2: "cancelled", 3: "pending", 4: "processing"}
    with sqlite3.connect(cards) as c:
        rows = dict(c.execute("SELECT key,status FROM card_jobs"))
    assert rows["catalog-a"] == "cancelled"
    assert rows["catalog-b"] == "pending"  # processing в общей очереди не дёргаем
    with sqlite3.connect(state) as c:
        items = dict(c.execute("SELECT key,status FROM excel_items"))
    assert items["excel-a"] == "cancelled"
    assert items["excel-new"] == "cancelled"
    assert items["excel-done"] == "preview"


def test_generation_on_does_not_resurrect_cancelled_tail(tmp_path):
    state = _state_db(tmp_path / "state.db")
    cards = _card_jobs_db(tmp_path / "card_jobs.db")
    queue = _queue_db(tmp_path / "queue.db")
    generation_command("off", state, cards, queue)

    reply = generation_command("on", state, cards, queue)

    assert generation_enabled(state) is True
    assert "включена" in reply.lower()
    with sqlite3.connect(queue) as c:
        assert c.execute("SELECT status FROM jobs WHERE id=1").fetchone()[0] == "cancelled"


def test_generation_status_reports_master_switch(tmp_path):
    state = tmp_path / "state.db"
    reply = generation_command(None, state, tmp_path / "cards.db", tmp_path / "queue.db")
    assert "ВЫКЛЮЧЕНА" in reply
    assert "/generation on" in reply


def test_catalog_timer_exits_before_scanning_stock_when_master_is_off(tmp_path, monkeypatch):
    from content_factory import cards_run

    state = tmp_path / "state.db"
    monkeypatch.setattr(cards_run, "load_config", lambda _: SimpleNamespace(
        state=SimpleNamespace(db=str(state))))
    monkeypatch.setattr(cards_run, "fetch_raw_products", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("склад не должен сканироваться при выключенном мастере")))

    assert cards_run.main() is None


def test_excel_timer_exits_before_pipeline_when_master_is_off(tmp_path, monkeypatch):
    from content_factory.orchestrator import excel_run

    state = tmp_path / "state.db"
    monkeypatch.setattr(excel_run, "load_config", lambda _: SimpleNamespace(
        state=SimpleNamespace(db=str(state))))
    monkeypatch.setattr(excel_run, "ExcelStore", lambda *_: (_ for _ in ()).throw(
        AssertionError("Excel-конвейер не должен стартовать при выключенном мастере")))

    assert excel_run.main() is None
