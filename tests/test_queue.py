from content_factory.orchestrator.tasks import Task
from content_factory.orchestrator.queue import TaskQueue


def _task(**kw):
    base = dict(id="t1", filter={"categories": [2], "source": "breeze"}, count=10,
                mode="mcp", schedule=["2026-06-26 10:00", "2026-06-26 14:00"],
                channel="@chan", confirm=True)
    base.update(kw)
    return Task(**base)


def test_add_expands_schedule_into_slots(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    q.add(_task())
    slots = q.all_slots()
    assert len(slots) == 2
    assert {s.due_at for s in slots} == {"2026-06-26 10:00", "2026-06-26 14:00"}
    assert all(s.status == "pending" for s in slots)


def test_due_returns_only_matured(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    q.add(_task())
    due = q.due("2026-06-26 11:00")           # между 10:00 и 14:00
    assert [s.due_at for s in due] == ["2026-06-26 10:00"]


def test_empty_schedule_is_immediate(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    q.add(_task(id="now", schedule=[]))
    due = q.due("2000-01-01 00:00")
    assert len(due) == 1 and due[0].task_id == "now"


def test_mark_done_not_returned(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    q.add(_task())
    q.mark_done("t1", "2026-06-26 10:00")
    due = q.due("2026-06-26 23:00")
    assert [s.due_at for s in due] == ["2026-06-26 14:00"]   # 10:00 уже done


def test_add_idempotent_preserves_done(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    q.add(_task())
    q.mark_done("t1", "2026-06-26 10:00")
    q.add(_task())                            # повторный add не должен воскрешать done
    statuses = {s.due_at: s.status for s in q.all_slots()}
    assert statuses["2026-06-26 10:00"] == "done"
    assert statuses["2026-06-26 14:00"] == "pending"
    assert len(q.all_slots()) == 2            # дублей нет


def test_slot_carries_filter_and_flags(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    q.add(_task())
    s = q.due("2026-06-26 10:00")[0]
    assert s.filter == {"categories": [2], "source": "breeze"}
    assert s.confirm is True and s.mode == "mcp" and s.channel == "@chan" and s.count == 10
