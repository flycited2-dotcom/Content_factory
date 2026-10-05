"""Owner selection after cancellation must not duplicate paid work or lose history."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from content_factory.orchestrator.excel_pipeline import ExcelStore, tick
from content_factory.orchestrator.confirm_store import ConfirmStore
from content_factory.publish.telegram import PublishState


def _row(key, price=1000):
    return (key, "Beko", key.rsplit("|", 1)[-1], "Стиральная машина " + key, price)


def _queue(path, jobs):
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE jobs(id INTEGER PRIMARY KEY,status TEXT,output_filename TEXT)")
        c.executemany("INSERT INTO jobs VALUES(?,?,?)", jobs)
    return path


def _cancelled(store, key, **fields):
    row = _row(key)
    store.add_items([row])
    store.update(key, status="cancelled", **fields)
    return row


def test_six_cancelled_washers_are_selectable_without_resurrecting_other_work(tmp_path):
    store = ExcelStore(tmp_path / "state.db")
    washers = [_cancelled(store, f"excel|beko|wm{i}") for i in range(6)]
    untouched = _cancelled(store, "excel|beko|old")
    juice = _row("excel|beko|juicer")
    store.add_items([juice])
    store.update(juice[0], status="research", research_job=123)

    assert not {row[0] for row in washers} & store.selection_blocked_keys()
    assert juice[0] in store.selection_blocked_keys()
    accepted = store.select_items(washers)

    assert accepted == washers
    assert len(store.by_status("new")) == 6
    assert store.get(untouched[0]).status == "cancelled"
    assert store.get(juice[0]).research_job == 123
    assert store.get(juice[0]).status == "research"
    receipt = store.latest_selection()
    assert receipt["requested"] == 6
    assert receipt["keys"] == [row[0] for row in washers]
    assert store.select_items(washers) == []
    assert store.latest_selection() == receipt
    assert len(store.all_keys()) == 8  # Full history still includes cancelled rows.


def test_import_add_items_still_does_not_revive_cancelled_owner_tasks(tmp_path):
    store = ExcelStore(tmp_path / "state.db")
    row = _cancelled(store, "excel|beko|wm1")
    assert store.add_items([row]) == 0
    assert store.get(row[0]).status == "cancelled"
    assert store.latest_selection() is None


@pytest.mark.parametrize("job_status", ["pending", "processing", "unknown", "done"])
def test_active_unknown_and_done_without_output_jobs_cannot_be_reselected(tmp_path, job_status):
    store = ExcelStore(tmp_path / "state.db")
    row = _cancelled(store, "excel|beko|wm1", card_job=10,
                     card_request_key="already-submitted")
    queue = _queue(tmp_path / "queue.db", [(10, job_status, None)])

    assert row[0] in store.selection_blocked_keys(queue)
    assert store.select_items([row], queue_db=queue, reservation=True) == []
    assert store.get(row[0]).status == "cancelled"
    with sqlite3.connect(queue) as c:
        assert c.execute("SELECT id,status,output_filename FROM jobs").fetchall() == [
            (10, job_status, None)]


@pytest.mark.parametrize("problem", ["missing_queue", "missing_job", "missing_table", "corrupt"])
def test_missing_or_unreadable_queue_evidence_fails_closed_without_creating_database(tmp_path, problem):
    store = ExcelStore(tmp_path / "state.db")
    row = _cancelled(store, "excel|beko|wm1", research_job=10)
    queue = tmp_path / "queue.db"
    if problem == "missing_job":
        _queue(queue, [])
    elif problem == "missing_table":
        with sqlite3.connect(queue) as c:
            c.execute("CREATE TABLE unrelated(value TEXT)")
    elif problem == "corrupt":
        queue.write_text("This is not SQLite.")

    assert row[0] in store.selection_blocked_keys(queue)
    assert store.select_items([row], queue_db=queue) == []
    if problem == "missing_queue":
        assert not queue.exists()


@pytest.mark.parametrize("request_column", ["research_request_key", "card_request_key"])
def test_timeout_with_request_key_but_no_persisted_job_id_is_not_safe_to_resubmit(tmp_path, request_column):
    store = ExcelStore(tmp_path / "state.db")
    row = _cancelled(store, "excel|beko|wm1", **{request_column: "uncertain-paid-request"})
    assert row[0] in store.selection_blocked_keys()
    assert store.select_items([row], reservation=True) == []
    assert store.get(row[0]).status == "cancelled"


@pytest.mark.parametrize("terminal_status", ["failed", "cancelled"])
def test_terminal_failed_or_cancelled_jobs_can_restart_with_fresh_keys_and_price(tmp_path, terminal_status):
    store = ExcelStore(tmp_path / "state.db")
    old = _cancelled(store, "excel|beko|wm1", research_job=10, card_job=11,
                     tries=2, error="old error", research_request_key="r1", card_request_key="c1")
    queue = _queue(tmp_path / "queue.db", [(10, terminal_status, None), (11, terminal_status, None)])
    fresh = _row(old[0], price=1234)

    assert old[0] not in store.selection_blocked_keys(queue)
    assert store.select_items([fresh], due_at=1000, queue_db=queue) == [fresh]
    item = store.get(old[0])
    assert item.status == "new"
    assert item.price == 1234
    assert item.research_job is None and item.card_job is None
    assert item.tries == 0 and item.error is None
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT due_at,research_request_key,card_request_key FROM excel_items").fetchone() == (
            1000, None, None)


@pytest.mark.parametrize("completed_stage", ["research", "card"])
def test_completed_result_resumes_existing_stage_and_preserves_cache_even_for_photo_override(tmp_path, completed_stage):
    store = ExcelStore(tmp_path / "state.db")
    row = _cancelled(store, "excel|beko|wm1", research_job=10, card_job=11,
                     research_request_key="r1", card_request_key="c1")
    store.cache_put("beko|wm1", "manual UTP", "source.png", source="manual")
    queue = _queue(tmp_path / "queue.db", [
        (10, "done", "source.png"),
        (11, "done" if completed_stage == "card" else "cancelled",
         "card.png" if completed_stage == "card" else None),
    ])

    assert row[0] not in store.selection_blocked_keys(queue)
    assert store.select_items([row], queue_db=queue, reservation=True) == [row]
    item = store.get(row[0])
    assert item.status == completed_stage  # Caller must skip a new override submit.
    assert item.research_job == 10
    assert item.card_job == (11 if completed_stage == "card" else None)
    assert store.cache_get("beko|wm1") == ("manual UTP", "source.png")
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT research_request_key,card_request_key FROM excel_items").fetchone() == (
            "r1", "c1" if completed_stage == "card" else None)


def test_photo_reservation_is_claimed_once_and_never_falls_through_to_research(tmp_path):
    store = ExcelStore(tmp_path / "state.db")
    row = _row("excel|beko|wm1")
    assert store.select_items([row, row], reservation=True) == [row]
    assert store.get(row[0]).status == "submission"
    assert store.latest_selection()["requested"] == 2
    assert store.select_items([row], reservation=True) == []

    def unexpected(*args, **kwargs):
        raise AssertionError("A reserved or uncertain submission must not generate anything.")

    assert tick(store, unexpected, unexpected, unexpected, unexpected) == {
        "research": 0, "card": 0, "preview": 0, "failed": 0}
    store.update(row[0], status="submission_failed", error="network outcome unknown")
    assert row[0] in store.selection_blocked_keys()
    assert store.retry_failed() == 0
    assert store.select_items([row], reservation=True) == []
    assert tick(store, unexpected, unexpected, unexpected, unexpected)["research"] == 0


def test_ready_price_and_completed_items_are_never_resurrected_by_manual_selection(tmp_path):
    store = ExcelStore(tmp_path / "state.db")
    ready = _cancelled(store, "ready-price|wm1")
    completed = _row("excel|beko|done")
    store.add_items([completed])
    store.update(completed[0], status="preview", card_job=10)
    assert ready[0] in store.selection_blocked_keys()
    assert completed[0] in store.selection_blocked_keys()
    assert store.select_items([ready, completed, _row("ready-price|new")]) == []
    assert store.get("ready-price|new") is None
    assert store.get(completed[0]).card_job == 10


@pytest.mark.parametrize("decision", ["pending", "rejected", "published"])
def test_stale_selection_cannot_override_existing_preview_decisions(tmp_path, decision):
    store = ExcelStore(tmp_path / "state.db")
    row = _cancelled(store, "excel|beko|wm1")
    confirms = ConfirmStore(store.path)
    confirms.add(row[0], "@channel", "card.png", "caption")
    confirms.mark(row[0], decision)
    assert row[0] in store.selection_blocked_keys()
    assert store.select_items([row], reservation=True) == []


def test_stale_selection_cannot_resubmit_an_already_published_key(tmp_path):
    store = ExcelStore(tmp_path / "state.db")
    row = _row("excel|beko|wm1")
    PublishState(store.path).mark(row[0], 123)
    assert store.select_items([row], reservation=True) == []
    assert store.get(row[0]) is None


def test_two_concurrent_confirmations_accept_one_cohort_and_write_one_receipt(tmp_path):
    store = ExcelStore(tmp_path / "state.db")
    rows = [_row(f"excel|beko|wm{i}") for i in range(6)]
    barrier = Barrier(2)

    def confirm():
        barrier.wait()
        return store.select_items(rows, reservation=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(confirm) for _ in range(2)]
        results = [future.result() for future in futures]
    assert sorted(len(result) for result in results) == [0, 6]
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT COUNT(*) FROM excel_selections").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM excel_items WHERE status='submission'").fetchone()[0] == 6


def test_selection_error_rolls_back_items_and_receipt_together(tmp_path):
    store = ExcelStore(tmp_path / "state.db")
    good = _row("excel|beko|wm1")
    bad = _row("excel|beko|wm2", price=[123])  # Unsupported SQL value after the first insert.
    with pytest.raises(sqlite3.ProgrammingError):
        store.select_items([good, bad])
    assert store.all_keys() == set()
    assert store.latest_selection() is None


@pytest.mark.parametrize("submission", ["research", "cached_card", "research_to_card"])
def test_cancellation_during_paid_submit_keeps_item_cancelled_and_retains_returned_job(tmp_path, submission):
    store = ExcelStore(tmp_path / "state.db")
    row = _row("excel|beko|wm1")
    store.add_items([row])
    if submission == "cached_card":
        store.cache_put("beko|wm1", "existing UTP", "reference.png")
    if submission == "research_to_card":
        store.update(row[0], status="research", research_job=100,
                     research_request_key="completed-research-request", tries=1)
    calls = []

    def cancel_before_response(*args, **kwargs):
        calls.append(kwargs["request_key"])
        store.update(row[0], status="cancelled")  # Owner presses Stop while HTTP is pending.
        return 201

    def existing_job(job_id):
        assert submission == "research_to_card" and job_id == 100
        return "done", "reference.png", "existing UTP", None

    def forbidden(*args, **kwargs):
        raise AssertionError("A cancelled item must not progress or publish.")

    stats = tick(store,
                 cancel_before_response if submission == "research" else forbidden,
                 existing_job,
                 cancel_before_response if submission != "research" else forbidden,
                 forbidden)

    assert len(calls) == 1
    assert stats == {"research": 0, "card": 0, "preview": 0, "failed": 0}
    item = store.get(row[0])
    assert item.status == "cancelled"
    if submission == "research":
        assert item.research_job == 201 and item.card_job is None
    else:
        assert item.card_job == 201
    if submission == "research_to_card":
        assert item.research_job == 100 and item.tries == 1
        with sqlite3.connect(store.path) as c:
            assert c.execute("SELECT research_request_key FROM excel_items WHERE key=?",
                             (row[0],)).fetchone()[0] == "completed-research-request"
        assert store.cache_get("beko|wm1") == ("existing UTP", "reference.png")
    # Even the next timer pass must not turn the returned paid job into new work.
    assert tick(store, forbidden, forbidden, forbidden, forbidden) == {
        "research": 0, "card": 0, "preview": 0, "failed": 0}


def test_returned_job_cannot_replace_a_newer_request_identity(tmp_path):
    store = ExcelStore(tmp_path / "state.db")
    row = _row("excel|beko|wm1")
    store.add_items([row])
    store.update(row[0], research_request_key="newer-request")
    assert not store.bind_submitted_job(row[0], "research", 200, "new",
                                        request_key="older-request")
    assert store.get(row[0]).status == "new"
    assert store.get(row[0]).research_job is None


def test_legitimate_research_to_card_binding_advances_and_clears_only_finished_request(tmp_path):
    store = ExcelStore(tmp_path / "state.db")
    row = _row("excel|beko|wm1")
    store.add_items([row])
    store.update(row[0], status="research", research_job=100, tries=1,
                 research_request_key="research-request", card_request_key="card-request")
    assert store.bind_submitted_job(row[0], "card", 200, "research", tries=0,
                                    request_key="card-request")
    item = store.get(row[0])
    assert item.status == "card" and item.card_job == 200 and item.research_job == 100
    assert item.tries == 0
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT research_request_key,card_request_key FROM excel_items WHERE key=?",
                         (row[0],)).fetchone() == (None, "card-request")
