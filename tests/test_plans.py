import pytest
from content_factory.orchestrator.plans import load_plans, load_plans_into_queue
from content_factory.orchestrator.queue import TaskQueue


def _write(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


GOOD = (
    "tasks:\n"
    "  - id: ac-morning\n"
    "    filter: {categories: [2], source: breeze}\n"
    "    count: 10\n"
    "    mode: mcp\n"
    "    channel: '@chan'\n"
    "    confirm: true\n"
    "    schedule: ['2026-06-26 10:00', '2026-06-26 14:00']\n"
)


def test_load_plans_from_file(tmp_path):
    tasks = load_plans(_write(tmp_path, "p.yaml", GOOD))
    assert len(tasks) == 1
    t = tasks[0]
    assert t.id == "ac-morning" and t.count == 10 and t.mode == "mcp"
    assert t.filter == {"categories": [2], "source": "breeze"}
    assert t.channel == "@chan" and t.confirm is True
    assert t.schedule == ["2026-06-26 10:00", "2026-06-26 14:00"]


def test_load_plans_from_dir(tmp_path):
    d = tmp_path / "tasks"
    d.mkdir()
    _write(d, "a.yaml", GOOD)
    _write(d, "b.yaml", GOOD.replace("ac-morning", "ac-2"))
    tasks = load_plans(d)
    assert {t.id for t in tasks} == {"ac-morning", "ac-2"}


def test_load_plans_into_queue(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    n = load_plans_into_queue(_write(tmp_path, "p.yaml", GOOD), q)
    assert n == 1
    assert len(q.all_slots()) == 2          # два времени → два слота


def test_missing_count_raises(tmp_path):
    bad = "tasks:\n  - id: x\n    filter: {categories: [2]}\n    schedule: ['2026-06-26 10:00']\n"
    with pytest.raises(ValueError, match="count"):
        load_plans(_write(tmp_path, "bad.yaml", bad))


def test_cron_schedule_clear_error(tmp_path):
    bad = ("tasks:\n  - id: x\n    filter: {categories: [2]}\n    count: 5\n"
           "    schedule: {cron: '0 11 * * 1-5'}\n")
    with pytest.raises(ValueError, match="cron"):
        load_plans(_write(tmp_path, "cron.yaml", bad))


def test_defaults_applied(tmp_path):
    minimal = ("tasks:\n  - id: m\n    filter: {categories: [2]}\n    count: 3\n"
               "    schedule: ['2026-06-26 10:00']\n")
    t = load_plans(_write(tmp_path, "m.yaml", minimal))[0]
    assert t.mode == "mcp" and t.confirm is False and t.channel == ""
