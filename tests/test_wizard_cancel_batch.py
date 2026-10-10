"""Complete owner wizard → latest batch → cancel → select again regressions."""
import sqlite3
from datetime import datetime

import openpyxl
import pytest

from content_factory.bot.run import make_cancel_excel_fn
from content_factory.bot.task_status import recover_submissions, selection_status_lines
from content_factory.bot.wizard import WizardStore
from content_factory.bot.wizard_flow import make_wizard_flow
from content_factory.orchestrator.excel_pipeline import ExcelStore


CATEGORY = "Стиральные машины с фронтальной загрузкой"


def _fixture(tmp_path, timeout_after_accept=False):
    prices = tmp_path / "prices"
    prices.mkdir()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["№", "Артикул", "Бренд", "Наименование", "Цена (руб.)", "Заказ (шт.)"])
    ws.append([CATEGORY, "", "", "", "", ""])
    for i in range(1, 7):
        ws.append([str(i), f"ART{i}", "Beko", f"Стиральная машина Beko WM{i}", 25000 + i, ""])
    wb.save(prices / "manual.xlsx")
    wb.close()

    state = tmp_path / "state.db"
    excel = ExcelStore(state)
    old_key = "excel|beko|juicer"
    excel.add_items([(old_key, "Beko", "Juicer", "Соковыжималка Beko Juicer", 7000)])
    excel.update(old_key, status="research", research_job=300)
    queue = tmp_path / "queue.db"
    with sqlite3.connect(queue) as c:
        c.execute("CREATE TABLE jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,status TEXT,"
                  "output_filename TEXT,request_key TEXT UNIQUE)")
        c.execute("INSERT INTO jobs VALUES(300,'processing',NULL,'old-juicer')")
    wizard = WizardStore(tmp_path / "wizard.db")
    paid_calls = []

    def submit_card(brand, model, utp, photo, *, request_key):
        paid_calls.append((brand, model, photo, request_key))
        with sqlite3.connect(queue) as c:
            job_id = c.execute("INSERT INTO jobs(status,request_key) VALUES('pending',?)",
                               (request_key,)).lastrowid
        if timeout_after_accept:
            raise TimeoutError("The paid queue accepted the request; response was lost.")
        return job_id

    def save_photo(chat, data):
        path = tmp_path / f"reference-{chat}.jpg"
        path.write_bytes(data)
        return str(path)

    flow = make_wizard_flow(state, prices, wizard, submit_card, save_photo,
                           lambda: "\n".join(selection_status_lines(excel)),
                           now_fn=lambda: datetime(2026, 10, 2, 0, 20), queue_db=queue)
    return flow, wizard, excel, queue, paid_calls, old_key


def _draft(flow, wizard, chat, count=6, photo=False):
    start, text, receive_photo, callback = flow
    sources = start(chat)
    source = next(button for row in sources.markup["inline_keyboard"] for button in row
                  if button["callback_data"].startswith("wizard:source:"))
    menu = callback(chat, source["callback_data"])
    button = next(button for row in menu.markup["inline_keyboard"] for button in row
                  if button["text"] == CATEGORY)
    listed = callback(chat, button["callback_data"])
    assert "доступно 6" in listed.text
    assert len(wizard.snapshot(chat).candidates) == 6
    text(chat, " ".join(str(i) for i in range(1, count + 1)))
    callback(chat, "wizard:time_now")
    if photo:
        receive_photo(chat, b"Owner supplied reference bytes")
    else:
        callback(chat, "wizard:skip_photo")
    callback(chat, "wizard:skip_utp")
    assert wizard.snapshot(chat).step == "awaiting_confirm"
    return callback


def _active_keys(excel):
    with sqlite3.connect(excel.path) as c:
        return {row[0] for row in c.execute("SELECT key FROM excel_items WHERE status IN "
                                            "('new','research','card','submission','submission_failed')")}


def test_six_washers_cancel_latest_and_select_again_preserves_the_other_active_task(tmp_path):
    flow, wizard, excel, queue, paid_calls, old_key = _fixture(tmp_path)
    callback = _draft(flow, wizard, "owner")
    reply = callback("owner", "wizard:confirm")
    assert "поставлено в очередь: 6" in reply.text
    for i in range(1, 7):
        assert f"Стиральная машина Beko WM{i}" in reply.text
    first = excel.latest_selection()
    assert first["requested"] == 6 and len(first["keys"]) == 6
    assert len(_active_keys(excel)) == 7
    status = "\n".join(selection_status_lines(excel))
    assert ": 6 товаров" in status
    assert "Другие задания Контент-завода в работе: 1." in status
    assert "ждут 6" in status and "поиск 0" in status

    # A second tap on the old confirm is harmless and must not change the receipt.
    callback("owner", "wizard:confirm")
    assert excel.latest_selection() == first
    assert len(_active_keys(excel)) == 7
    assert paid_calls == []

    cancel = make_cancel_excel_fn(excel.path, queue)
    cancelled = cancel("latest")
    assert "отменено 6" in cancelled
    assert {excel.get(key).status for key in first["keys"]} == {"cancelled"}
    assert excel.get(old_key).status == "research"
    assert excel.get(old_key).research_job == 300
    with sqlite3.connect(queue) as c:
        assert c.execute("SELECT status FROM jobs WHERE id=300").fetchone()[0] == "processing"
    assert _active_keys(excel) == {old_key}

    callback = _draft(flow, wizard, "owner")
    assert {row[0] for row in wizard.snapshot("owner").candidates} == set(first["keys"])
    reply = callback("owner", "wizard:confirm")
    assert "поставлено в очередь: 6" in reply.text
    latest = excel.latest_selection()
    assert latest["id"] > first["id"]
    assert latest["requested"] == 6 and latest["keys"] == first["keys"]
    assert len(_active_keys(excel)) == 7
    assert len(excel.all_keys()) == 7  # No second group of six was inserted.
    assert excel.get(old_key).status == "research"
    assert paid_calls == []


def test_stale_photo_draft_after_another_confirmation_adds_zero_without_another_paid_request(tmp_path):
    flow, wizard, excel, queue, paid_calls, _ = _fixture(tmp_path)
    callback = _draft(flow, wizard, "stale", count=1, photo=True)
    _draft(flow, wizard, "winner", count=1, photo=True)
    winner = callback("winner", "wizard:confirm")
    assert "поставлено в очередь: 1" in winner.text
    receipt = excel.latest_selection()
    assert len(paid_calls) == 1

    stale = callback("stale", "wizard:confirm")
    assert "Новых задач добавлено: 0" in stale.text
    assert len(paid_calls) == 1
    assert excel.latest_selection() == receipt
    assert wizard.snapshot("stale") is None
    with sqlite3.connect(queue) as c:
        assert c.execute("SELECT COUNT(*) FROM jobs WHERE request_key<>'old-juicer'").fetchone()[0] == 1
    assert len(_active_keys(excel)) == 2


def test_timed_out_photo_submission_recovers_existing_request_and_cancels_without_duplicate(tmp_path):
    flow, wizard, excel, queue, paid_calls, old_key = _fixture(tmp_path, timeout_after_accept=True)
    callback = _draft(flow, wizard, "owner", count=1, photo=True)
    with pytest.raises(TimeoutError):
        callback("owner", "wizard:confirm")
    key = excel.latest_selection()["keys"][0]
    assert excel.get(key).status == "submission_failed"
    assert excel.get(key).card_job is None
    assert len(paid_calls) == 1

    # A second confirm cannot buy the uncertain request again.
    repeat = callback("owner", "wizard:confirm")
    assert "Новых задач добавлено: 0" in repeat.text
    assert len(paid_calls) == 1
    assert recover_submissions(excel, queue) == 1
    job_id = excel.get(key).card_job
    assert excel.get(key).status == "card" and job_id is not None
    assert recover_submissions(excel, queue) == 0
    assert len(paid_calls) == 1

    reply = make_cancel_excel_fn(excel.path, queue)("latest")
    assert "отменено 1" in reply
    assert excel.get(key).status == "cancelled"
    assert excel.get(old_key).status == "research"
    with sqlite3.connect(queue) as c:
        assert c.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0] == "cancelled"
        assert c.execute("SELECT status FROM jobs WHERE id=300").fetchone()[0] == "processing"
    assert recover_submissions(excel, queue) == 0
    assert len(paid_calls) == 1
