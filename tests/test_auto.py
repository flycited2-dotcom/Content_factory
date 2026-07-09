from datetime import date, datetime
import pytest
from content_factory.orchestrator.auto import (
    auto_command, auto_enabled, effective_auto_tasks, materialize_auto_tasks,
    maybe_materialize, resolve_cats, set_auto_enabled)
from content_factory.orchestrator.queue import TaskQueue
from content_factory.orchestrator.tasks import Task

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


def test_maybe_materialize_off_cancels_auto_keeps_manual(tmp_path):
    # флаг не выставлен = ВЫКЛ: не материализуем И отменяем уже созданные авто-слоты
    q = TaskQueue(tmp_path / "q.db")
    materialize_auto_tasks(CFG, date(2026, 7, 2), q)         # авто-слоты уже в очереди
    q.add(Task(id="manual-1", filter={}, count=1, mode="mcp",
               schedule=["2026-07-02 12:00"], channel="", confirm=True))
    tasks = maybe_materialize(CFG, date(2026, 7, 2), q, tmp_path / "s.db")
    assert tasks == []
    st = {s.task_id: s.status for s in q.all_slots()}
    assert st["manual-1"] == "pending"                       # ручные неприкосновенны
    assert st["auto-ac-2026-07-02"] == "cancelled"


def test_maybe_materialize_on_materializes(tmp_path):
    db = tmp_path / "s.db"
    set_auto_enabled(db, True)
    q = TaskQueue(tmp_path / "q.db")
    tasks = maybe_materialize(CFG, date(2026, 7, 2), q, db)
    assert [t.id for t in tasks] == ["auto-ac-2026-07-02"]
    assert len(q.all_slots()) == 2


def _q_with_auto(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    materialize_auto_tasks(CFG, date(2026, 7, 2), q)
    return q


def test_auto_command_status_off_by_default(tmp_path):
    q = _q_with_auto(tmp_path)
    txt = auto_command(None, CFG, q, tmp_path / "s.db", datetime(2026, 7, 2, 9, 0))
    assert "ВЫКЛЮЧЕН" in txt and "/auto on" in txt
    assert "ac: 10:00, 14:00 × 2" in txt                    # расписание из конфига
    assert "pending 2" in txt                               # слоты сегодня


def test_auto_command_off_flags_and_cancels(tmp_path):
    q = _q_with_auto(tmp_path)
    db = tmp_path / "s.db"
    set_auto_enabled(db, True)
    txt = auto_command("off", CFG, q, db, datetime(2026, 7, 2, 9, 0))
    assert "выключен" in txt and "Отменено слотов: 2" in txt
    assert auto_enabled(db) is False
    assert all(s.status == "cancelled" for s in q.all_slots())


def test_auto_command_on_resurrects_only_future(tmp_path):
    q = _q_with_auto(tmp_path)
    db = tmp_path / "s.db"
    auto_command("off", CFG, q, db, datetime(2026, 7, 2, 9, 0))
    txt = auto_command("on", CFG, q, db, datetime(2026, 7, 2, 12, 0))   # 10:00 уже прошло
    assert "включён" in txt and "Сегодня ещё слотов: 1" in txt
    assert auto_enabled(db) is True
    st = {s.due_at: s.status for s in q.all_slots()}
    assert st == {"2026-07-02 10:00": "cancelled", "2026-07-02 14:00": "pending"}


def test_auto_command_on_without_config(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    txt = auto_command("on", [], q, tmp_path / "s.db", datetime(2026, 7, 2, 9, 0))
    assert "❌" in txt                                       # включать нечего


def test_materialize_invalid_config_raises(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    with pytest.raises(ValueError, match="times"):
        materialize_auto_tasks([{"id": "x", "count": 1}], date(2026, 7, 2), q)
    with pytest.raises(ValueError, match="id"):
        materialize_auto_tasks([{"count": 1, "times": ["09:00"]}], date(2026, 7, 2), q)
    with pytest.raises(ValueError, match="count"):
        materialize_auto_tasks([{"id": "x", "times": ["09:00"]}], date(2026, 7, 2), q)


def test_effective_auto_tasks_override_and_reset(tmp_path):
    # п.5 (2026-07-09): полный контроль автомата из бота — override поверх yaml
    db = tmp_path / "s.db"
    assert effective_auto_tasks(db, CFG) == CFG            # нет override — yaml
    auto_command("times 09:00, 13:30", CFG, TaskQueue(tmp_path / "q.db"), db,
                 datetime(2026, 7, 2, 8, 0))
    eff = effective_auto_tasks(db, CFG)
    assert len(eff) == 1 and eff[0]["times"] == ["09:00", "13:30"]
    assert eff[0]["filter"] == CFG[0]["filter"]            # категории унаследованы
    assert eff[0]["count"] == CFG[0]["count"]
    auto_command("reset", CFG, TaskQueue(tmp_path / "q.db"), db,
                 datetime(2026, 7, 2, 8, 0))
    assert effective_auto_tasks(db, CFG) == CFG


def test_auto_command_edit_count_and_cats(tmp_path):
    db = tmp_path / "s.db"
    q = TaskQueue(tmp_path / "q.db")
    txt = auto_command("count 3", CFG, q, db, datetime(2026, 7, 2, 8, 0))
    assert "3" in txt
    txt = auto_command("cats 2,6,7", CFG, q, db, datetime(2026, 7, 2, 8, 0))
    assert "2, 6, 7" in txt or "2,6,7" in txt
    eff = effective_auto_tasks(db, CFG)
    assert eff[0]["count"] == 3
    assert eff[0]["filter"] == {"categories": [2, 6, 7]}
    assert "❌" in auto_command("times ерунда", CFG, q, db, datetime(2026, 7, 2, 8, 0))
    assert "❌" in auto_command("count ноль", CFG, q, db, datetime(2026, 7, 2, 8, 0))


def test_maybe_materialize_uses_override(tmp_path):
    db = tmp_path / "s.db"
    q = TaskQueue(tmp_path / "q.db")
    set_auto_enabled(db, True)
    auto_command("times 11:00", CFG, q, db, datetime(2026, 7, 2, 8, 0))
    auto_command("count 4", CFG, q, db, datetime(2026, 7, 2, 8, 0))
    tasks = maybe_materialize(CFG, date(2026, 7, 2), q, db)
    assert len(tasks) == 1
    slots = [s for s in q.all_slots() if s.status == "pending"]
    assert [(s.due_at, s.count) for s in slots] == [("2026-07-02 11:00", 4)]


CATALOG = {2: "Бытовые сплит-системы", 6: "Полупромышленные сплит-системы",
           7: "Мобильные кондиционеры", 26: "Тепловые пушки"}


def test_resolve_cats_words_and_numbers():
    # владелец 2026-07-09: «хочу писать словами, а не id»
    ids, unknown = resolve_cats("мобильные кондиционеры, 26", CATALOG)
    assert ids == [7, 26] and unknown == []
    ids, unknown = resolve_cats("сплит-системы", CATALOG)
    assert ids == [2, 6]                                   # с окончаниями, все матчи
    ids, unknown = resolve_cats("чайник", CATALOG)
    assert ids == [] and unknown == ["чайник"]             # нет в БД склада


def test_auto_command_cats_by_words(tmp_path):
    db = tmp_path / "s.db"
    q = TaskQueue(tmp_path / "q.db")
    txt = auto_command("cats тепловые пушки", CFG, q, db, datetime(2026, 7, 2, 8, 0),
                       catalog_fn=lambda: CATALOG)
    assert "Тепловые пушки" in txt
    assert effective_auto_tasks(db, CFG)[0]["filter"] == {"categories": [26]}
    txt = auto_command("cats чайник", CFG, q, db, datetime(2026, 7, 2, 8, 0),
                       catalog_fn=lambda: CATALOG)
    assert "❌" in txt and "чайник" in txt                 # что не понято — сказано


def test_auto_edit_reply_includes_schedule(tmp_path):
    # UX 2026-07-09: после правки сразу видно итоговое расписание
    db = tmp_path / "s.db"
    q = TaskQueue(tmp_path / "q.db")
    txt = auto_command("times 20:30", CFG, q, db, datetime(2026, 7, 2, 8, 0))
    assert "20:30" in txt and "Расписание" in txt
