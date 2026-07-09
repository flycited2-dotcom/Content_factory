from datetime import date
import pytest
from content_factory.orchestrator.auto import (
    auto_enabled, materialize_auto_tasks, set_auto_enabled)
from content_factory.orchestrator.queue import TaskQueue

CFG = [{"id": "ac", "filter": {"categories": [2]}, "count": 2, "times": ["10:00", "14:00"]}]


def test_materialize_creates_slots_for_today(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    tasks = materialize_auto_tasks(CFG, date(2026, 7, 2), q)
    assert [t.id for t in tasks] == ["auto-ac-2026-07-02"]
    slots = q.all_slots()
    assert [(s.due_at, s.count, s.confirm) for s in slots] == [
        ("2026-07-02 10:00", 2, True), ("2026-07-02 14:00", 2, True)]
    assert slots[0].filter == {"categories": [2]}


def test_materialize_idempotent_and_keeps_done(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    materialize_auto_tasks(CFG, date(2026, 7, 2), q)
    q.mark_done("auto-ac-2026-07-02", "2026-07-02 10:00")
    materialize_auto_tasks(CFG, date(2026, 7, 2), q)      # повторный тик
    slots = q.all_slots()
    assert len(slots) == 2                                # дублей нет
    assert {s.due_at: s.status for s in slots} == {
        "2026-07-02 10:00": "done", "2026-07-02 14:00": "pending"}


def test_materialize_next_day_new_slots(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    materialize_auto_tasks(CFG, date(2026, 7, 2), q)
    materialize_auto_tasks(CFG, date(2026, 7, 3), q)
    assert len(q.all_slots()) == 4                        # у каждого дня свой task_id


def test_materialize_confirm_off_and_mode(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    cfg = [{"id": "x", "filter": {}, "count": 1, "times": ["09:00"],
            "mode": "kbt", "confirm": False, "channel": "@special"}]
    (t,) = materialize_auto_tasks(cfg, date(2026, 7, 2), q)
    assert (t.mode, t.confirm, t.channel) == ("kbt", False, "@special")


def test_auto_enabled_default_off(tmp_path):
    # нет записи = ВЫКЛЮЧЕНО (решение владельца 2026-07-09: после деплоя автомат молчит)
    assert auto_enabled(tmp_path / "s.db") is False


def test_set_auto_enabled_roundtrip(tmp_path):
    db = tmp_path / "s.db"
    set_auto_enabled(db, True)
    assert auto_enabled(db) is True
    set_auto_enabled(db, False)
    assert auto_enabled(db) is False


def test_materialize_invalid_config_raises(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    with pytest.raises(ValueError, match="times"):
        materialize_auto_tasks([{"id": "x", "count": 1}], date(2026, 7, 2), q)
    with pytest.raises(ValueError, match="id"):
        materialize_auto_tasks([{"count": 1, "times": ["09:00"]}], date(2026, 7, 2), q)
    with pytest.raises(ValueError, match="count"):
        materialize_auto_tasks([{"id": "x", "times": ["09:00"]}], date(2026, 7, 2), q)
