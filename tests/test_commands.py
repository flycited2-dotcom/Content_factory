from datetime import date
import pytest
from content_factory.bot.commands import parse_plan, handle_command
from content_factory.orchestrator.queue import TaskQueue

TODAY = date(2026, 6, 25)


def test_parse_basic_command():
    t = parse_plan("/plan 10 кондиционеры завтра 10:00,14:00 mode=mcp", today=TODAY)
    assert t.count == 10
    assert t.filter == {"categories": [2, 6, 7]}
    assert t.mode == "mcp"
    assert t.schedule == ["2026-06-26 10:00", "2026-06-26 14:00"]
    assert t.confirm is False


def test_parse_segodnya_and_padding():
    t = parse_plan("/plan 5 кондиционеры сегодня 9:00", today=TODAY)
    assert t.schedule == ["2026-06-25 09:00"]


def test_parse_explicit_date():
    t = parse_plan("/plan 5 кондиционеры 2026-07-01 11:00", today=TODAY)
    assert t.schedule == ["2026-07-01 11:00"]


def test_parse_source_and_cat_override():
    t = parse_plan("/plan 8 завтра 10:00 source=breeze cat=2,6", today=TODAY)
    assert t.filter == {"categories": [2, 6], "source": "breeze"}


def test_parse_confirm_and_channel_and_id():
    t = parse_plan("/plan 3 кондиционеры завтра 10:00 confirm channel=@x id=myid", today=TODAY)
    assert t.confirm is True and t.channel == "@x" and t.id == "myid"


def test_parse_missing_count_raises():
    with pytest.raises(ValueError, match="количество"):
        parse_plan("/plan кондиционеры завтра 10:00", today=TODAY)


def test_parse_missing_time_raises():
    with pytest.raises(ValueError, match="врем"):
        parse_plan("/plan 5 кондиционеры завтра", today=TODAY)


def test_parse_missing_filter_raises():
    with pytest.raises(ValueError, match="категори"):
        parse_plan("/plan 5 завтра 10:00", today=TODAY)


def test_handle_plan_adds_to_queue(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    reply = handle_command("/plan 10 кондиционеры завтра 10:00,14:00 mode=mcp", q, today=TODAY)
    assert "✅" in reply or "ок" in reply.lower()
    assert len(q.all_slots()) == 2


def test_handle_status(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    handle_command("/plan 5 кондиционеры завтра 10:00 id=t1", q, today=TODAY)
    reply = handle_command("/status", q, today=TODAY)
    assert "t1" in reply or "1" in reply        # есть инфо о задаче/слотах


def test_handle_cancel(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    handle_command("/plan 5 кондиционеры завтра 10:00 id=t1", q, today=TODAY)
    reply = handle_command("/cancel t1", q, today=TODAY)
    assert "t1" in reply
    assert all(s.status == "cancelled" for s in q.all_slots())


def test_handle_unknown_returns_help(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    reply = handle_command("/foobar", q, today=TODAY)
    assert "/plan" in reply        # подсказка по командам


def test_handle_invalid_plan_returns_error(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    reply = handle_command("/plan завтра 10:00", q, today=TODAY)   # нет count
    assert "❌" in reply or "ошибк" in reply.lower()
    assert q.all_slots() == []
