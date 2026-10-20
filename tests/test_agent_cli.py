"""agent_cli — JSON-интерфейс постановки задач для оркестратора (Agents_MY, этап 2).

Оркестратор не пишет в базу сам: он вызывает эти подкоманды, а они используют
тот же код, что /make и кнопка «Отменить» в боте.
"""
import json
from datetime import datetime

import pytest

from content_factory import agent_cli
from content_factory.bot.commands import parse_due_at
from content_factory.orchestrator.excel_pipeline import ExcelStore


@pytest.fixture
def env(tmp_path):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["№", "Артикул", "Бренд", "Наименование", "Цена (руб.)", "Заказ (шт.)"])
    ws.append(["Холодильники", "", "", "", "", ""])
    ws.append(["1", "10", "Beko", "Холодильник Beko X100", "30000", ""])
    ws.append(["2", "11", "Candy", "Холодильник Candy Y200", "25000", ""])
    prices = tmp_path / "prices"
    prices.mkdir()
    wb.save(prices / "manual.xlsx")
    state = tmp_path / "state.db"
    base = ["--state-db", str(state), "--prices-dir", str(prices), "--queue-db", "", "--no-wake"]
    return state, base


def _run(capsys, args):
    code = agent_cli.main(args)
    return code, json.loads(capsys.readouterr().out)


def test_hint_lists_brands_from_current_prices(env, capsys):
    _, base = env
    code, out = _run(capsys, ["hint", *base])
    assert code == 0
    assert set(out["brands"]) == {"Beko", "Candy"}
    assert out["sources"] == ["manual"]


def test_resolve_has_no_side_effects(env, capsys):
    state, base = env
    code, out = _run(capsys, ["resolve", "--category", "холодильники", *base])
    assert code == 0
    assert out["total_matches"] == 2
    assert {i["key"] for i in out["items"]} == {"excel|beko|x100", "excel|candy|y200"}
    assert ExcelStore(state).by_status("new") == []


def test_resolve_count_limits_items_but_not_total(env, capsys):
    _, base = env
    _, out = _run(capsys, ["resolve", "--category", "холодильники", "--count", "1", *base])
    assert len(out["items"]) == 1
    assert out["total_matches"] == 2


def test_enqueue_selects_with_due_time(env, capsys):
    state, base = env
    now = datetime.now()
    code, out = _run(capsys, ["enqueue", "--category", "холодильники", "--due", "завтра 12:00", *base])
    assert code == 0
    assert set(out["accepted"]) == {"excel|beko|x100", "excel|candy|y200"}
    assert out["due_at"] == parse_due_at("завтра 12:00", now)
    # до срока задачи скрыты от тика (расписание /task), после — видны
    assert ExcelStore(state).by_status("new") == []
    assert {i.key for i in ExcelStore(state).by_status("new", now=out["due_at"] + 1)} == set(out["accepted"])


def test_enqueue_twice_accepts_nothing_second_time(env, capsys):
    _, base = env
    _run(capsys, ["enqueue", "--category", "холодильники", *base])
    _, out = _run(capsys, ["enqueue", "--category", "холодильники", *base])
    assert out["accepted"] == []


def test_cancel_marks_items_cancelled(env, capsys):
    state, base = env
    _, enq = _run(capsys, ["enqueue", "--category", "холодильники", *base])
    code, out = _run(capsys, ["cancel", "--keys", ",".join(enq["accepted"]), *base])
    assert code == 0
    assert out["cancelled"] == 2
    assert ExcelStore(state).by_status("new") == []


def test_unknown_command_or_bad_quotas_exit_2(env, capsys):
    _, base = env
    with pytest.raises(SystemExit) as exc:
        agent_cli.main(["nope", *base])
    assert exc.value.code == 2
    capsys.readouterr()
    code, out = _run(capsys, ["resolve", "--category", "x", "--quotas", "{bad", *base])
    assert code == 2
    assert "error" in out


def test_brand_all_quota_limits_to_that_brand(env, capsys):
    # «все холодильники beko» → {"beko": null} без count: только Beko, а не вся категория
    _, base = env
    _, out = _run(capsys, ["resolve", "--category", "холодильники", "--quotas", '{"beko": null}', *base])
    assert [i["key"] for i in out["items"]] == ["excel|beko|x100"]
    assert out["total_matches"] == 1


def test_mixed_quotas_keep_numbered_brand_and_all_of_null_brand(env, capsys):
    # «1 Candy и все Beko» → {"candy": 1, "beko": null}: обе марки, без добора чужих
    _, base = env
    _, out = _run(capsys, ["resolve", "--category", "холодильники", "--quotas", '{"candy": 1, "beko": null}', *base])
    assert sorted(i["key"] for i in out["items"]) == ["excel|beko|x100", "excel|candy|y200"]
