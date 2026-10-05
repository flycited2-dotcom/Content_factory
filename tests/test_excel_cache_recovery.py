"""A stale cached image or one network failure must not strand a whole batch."""
import sqlite3

import pytest

from content_factory.orchestrator.excel_pipeline import ExcelStore, tick


def _store(tmp_path, count=6):
    store = ExcelStore(tmp_path / "state.db")
    rows = [(f"excel|beko|wm{i}", "Beko", f"WM{i}", f"Стиральная машина Beko WM{i}", 25000)
            for i in range(1, count + 1)]
    store.add_items(rows)
    return store, rows


def _cached(store, rows):
    for key, brand, model, _, _ in rows:
        store.cache_put(f"{brand.lower()}|{model.lower()}", "existing UTP", model + ".png")


def _pending(job_id):
    return "pending", None, None, None


def _forbidden(*args, **kwargs):
    raise AssertionError("This stage must not be invoked.")


def test_missing_cached_photo_researches_only_that_item_and_processes_other_five(tmp_path):
    store, rows = _store(tmp_path)
    _cached(store, rows)
    key = rows[0][0]
    old_research_key = store.get_or_create_request_key(key, "research")
    old_card_key = store.get_or_create_request_key(key, "card")
    calls = {"research": [], "card": []}

    def resolve(item, path):
        return None if item.model == "WM1" else path

    def research(brand, model, category, *, request_key):
        calls["research"].append((model, request_key))
        return 101

    def card(brand, model, utp, photo, mode, *, request_key):
        calls["card"].append(model)
        return 200 + int(model[2:])

    stats = tick(store, research, _pending, card, _forbidden, resolve_photo=resolve)
    assert calls["research"] == [("WM1", old_research_key)]
    assert calls["card"] == [f"WM{i}" for i in range(2, 7)]
    assert stats == {"research": 1, "card": 5, "preview": 0, "failed": 0}
    assert store.get(key).status == "research"
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT research_request_key,card_request_key FROM excel_items WHERE key=?",
                         (key,)).fetchone() == (old_research_key, old_card_key)


def test_cached_submit_file_not_found_before_http_falls_back_without_aborting_batch(tmp_path):
    store, rows = _store(tmp_path)
    _cached(store, rows)
    calls = {"research": [], "card": []}

    def card(brand, model, utp, photo, mode, *, request_key):
        calls["card"].append((model, request_key))
        if model == "WM1":
            raise FileNotFoundError("cached file disappeared before HTTP")
        return 200 + int(model[2:])

    def research(brand, model, category, *, request_key):
        calls["research"].append(model)
        return 101

    stats = tick(store, research, _pending, card, _forbidden)
    assert calls["research"] == ["WM1"]
    assert len(calls["card"]) == 6
    assert stats["card"] == 5 and stats["research"] == 1
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT card_request_key FROM excel_items WHERE key=?",
                         (rows[0][0],)).fetchone()[0] == calls["card"][0][1]


def test_lost_card_response_is_isolated_and_retry_reuses_the_same_paid_identity(tmp_path, caplog):
    store, rows = _store(tmp_path)
    _cached(store, rows)
    accepted = {}
    calls = []

    def card(brand, model, utp, photo, mode, *, request_key):
        first = request_key not in accepted
        accepted.setdefault(request_key, 200 + int(model[2:]))
        calls.append((model, request_key))
        if model == "WM1" and first:
            raise TimeoutError("response lost token=SECRET https://private.test/SECRET")
        return accepted[request_key]

    first = tick(store, _forbidden, _pending, card, _forbidden)
    assert first["card"] == 5 and len(accepted) == 6
    item = store.get(rows[0][0])
    assert item.status == "new" and item.error == "card_submit:TimeoutError"
    assert "SECRET" not in caplog.text
    second = tick(store, _forbidden, _pending, card, _forbidden)
    assert second["card"] == 1 and len(accepted) == 6
    failed_calls = [request for model, request in calls if model == "WM1"]
    assert len(failed_calls) == 2 and failed_calls[0] == failed_calls[1]
    assert store.get(rows[0][0]).status == "card"
    assert store.get(rows[0][0]).error is None


def test_lost_research_response_preserves_identity_without_blocking_other_items(tmp_path):
    store, rows = _store(tmp_path, count=2)
    accepted = {}
    calls = []

    def research(brand, model, category, *, request_key):
        first = request_key not in accepted
        accepted.setdefault(request_key, 100 + int(model[2:]))
        calls.append((model, request_key))
        if model == "WM1" and first:
            raise ConnectionError("connection lost after acceptance")
        return accepted[request_key]

    stats = tick(store, research, _pending, _forbidden, _forbidden)
    assert stats["research"] == 1
    assert store.get(rows[0][0]).error == "research_submit:ConnectionError"
    assert store.get(rows[1][0]).status == "research"
    tick(store, research, _pending, _forbidden, _forbidden)
    assert len(accepted) == 2
    assert [request for model, request in calls if model == "WM1"] == [calls[0][1], calls[0][1]]


def test_research_done_with_missing_photo_has_a_bounded_retry(tmp_path):
    store, rows = _store(tmp_path, count=1)
    key = rows[0][0]
    store.update(key, status="research", research_job=100)
    research_calls = []

    def research(*args, **kwargs):
        research_calls.append(kwargs["request_key"])
        return 101

    def missing_job(job_id):
        return "done", "deleted.png", "verified UTP", None

    first = tick(store, research, missing_job, _forbidden, _forbidden,
                 resolve_photo=lambda item, photo: None)
    assert first["failed"] == 0 and store.get(key).status == "new"
    assert store.get(key).tries == 1
    events = []
    second = tick(store, research, missing_job, _forbidden, _forbidden,
                  resolve_photo=lambda item, photo: None, failed_events=events)
    assert second["failed"] == 1 and store.get(key).status == "failed"
    assert len(research_calls) == 1 and len(events) == 1
    tick(store, _forbidden, _forbidden, _forbidden, _forbidden)


def test_recovered_research_photo_keeps_manual_utp_priority(tmp_path):
    store, rows = _store(tmp_path, count=1)
    key = rows[0][0]
    store.cache_put("beko|wm1", "Owner verified UTP", None, source="manual")
    store.update(key, status="research", research_job=100)
    cards = []

    def read(job_id):
        if job_id == 100:
            return "done", "new-source.png", "Automatically researched UTP", None
        return _pending(job_id)

    def card(brand, model, utp, photo, mode, *, request_key):
        cards.append((utp, photo))
        return 200

    tick(store, _forbidden, read, card, _forbidden,
         resolve_photo=lambda item, photo: "/verified/archive/original.png")
    assert cards == [("Owner verified UTP", "/verified/archive/original.png")]
    assert store.cache_get("beko|wm1")[0] == "Owner verified UTP"


@pytest.mark.parametrize("stage", ["research", "card"])
def test_one_job_read_failure_preserves_its_stage_while_other_item_progresses(tmp_path, stage):
    store, rows = _store(tmp_path, count=2)
    for i, row in enumerate(rows, 1):
        store.update(row[0], status=stage, **{stage + "_job": 100 + i})

    def read(job_id):
        if job_id == 101:
            raise OSError("private connection details must not be stored")
        if job_id == 102:
            return "done", "source-or-card.png", "UTP", None
        return _pending(job_id)

    stats = tick(store, _forbidden, read, lambda *a, **k: 200, lambda *a: True)
    assert store.get(rows[0][0]).status == stage
    assert store.get(rows[0][0]).error == f"{stage}_read:OSError"
    assert store.get(rows[1][0]).status == ("card" if stage == "research" else "preview")
    assert stats["card" if stage == "research" else "preview"] == 1


def test_preview_failure_retries_only_preview_and_retains_paid_job(tmp_path):
    store, rows = _store(tmp_path, count=1)
    key = rows[0][0]
    store.update(key, status="card", card_job=200, card_request_key="existing-card-request")
    read = lambda job_id: ("done", "finished.png", None, None)

    def network_failure(*args):
        raise TimeoutError("private Telegram payload")

    first = tick(store, _forbidden, read, _forbidden, network_failure)
    assert first["preview"] == 0
    assert store.get(key).status == "card" and store.get(key).card_job == 200
    assert store.get(key).error == "preview:TimeoutError"
    second = tick(store, _forbidden, read, _forbidden, lambda *args: True)
    assert second["preview"] == 1 and store.get(key).status == "preview"


@pytest.mark.parametrize("accepted", [True, (False, "card text rejected")])
def test_owner_cancellation_wins_over_preview_success_or_audit_retry(tmp_path, accepted):
    store, rows = _store(tmp_path, count=1)
    key = rows[0][0]
    store.update(key, status="card", card_job=200, card_request_key="card-request")

    def stop_during_preview(*args):
        store.update(key, status="cancelled")
        return accepted

    stats = tick(store, _forbidden, lambda job_id: ("done", "finished.png", None, None),
                 _forbidden, stop_during_preview)
    assert stats == {"research": 0, "card": 0, "preview": 0, "failed": 0}
    assert store.get(key).status == "cancelled" and store.get(key).card_job == 200
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT card_request_key FROM excel_items WHERE key=?",
                         (key,)).fetchone()[0] == "card-request"


def test_owner_cancellation_during_failed_job_read_cannot_be_undone_by_retry(tmp_path):
    store, rows = _store(tmp_path, count=1)
    key = rows[0][0]
    store.update(key, status="research", research_job=100, research_request_key="request")

    def stop_during_read(job_id):
        store.update(key, status="cancelled")
        return "failed", None, None, "old error"

    tick(store, _forbidden, stop_during_read, _forbidden, _forbidden)
    assert store.get(key).status == "cancelled" and store.get(key).research_job == 100


def test_photo_resolver_failure_does_not_generate_blindly_or_block_other_cached_items(tmp_path):
    store, rows = _store(tmp_path, count=2)
    _cached(store, rows)
    cards = []

    def resolve(item, path):
        if item.model == "WM1":
            raise PermissionError("private archive path")
        return path

    def card(brand, model, *args, **kwargs):
        cards.append(model)
        return 200

    tick(store, _forbidden, _pending, card, _forbidden, resolve_photo=resolve)
    assert store.get(rows[0][0]).status == "new"
    assert store.get(rows[0][0]).error == "cache_photo:PermissionError"
    assert cards == ["WM2"]
