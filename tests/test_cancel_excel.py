"""Отмена задач excel-конвейера из бота (запрос владельца 2026-07-06: «нет
абсолютно никакой кнопки остановить задачу»). Кнопки под /excel-статусом:
отмена одного товара или всех активных; связанные pending-задачи в очереди
агента тоже отменяются (processing не трогаем — агент уже генерит, но товар
из конвейера снят и результат никуда не поедет)."""
import sqlite3

from content_factory.bot import run as botrun
from content_factory.orchestrator.excel_pipeline import ExcelStore


def _queue_db(tmp_path, jobs):
    db = tmp_path / "q.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, status TEXT)")
    for jid, status in jobs:
        con.execute("INSERT INTO jobs(id, status) VALUES (?, ?)", (jid, status))
    con.commit()
    con.close()
    return str(db)


def _job_status(db, jid):
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0]
    finally:
        con.close()


def test_cancel_one_item_cancels_item_and_pending_job(tmp_path):
    state_db = tmp_path / "state.db"
    es = ExcelStore(state_db)
    es.add_items([("excel|lg|x1", "LG", "X1", "Холодильник LG X1", 100),
                  ("excel|lg|x2", "LG", "X2", "Холодильник LG X2", 200)])
    es.update("excel|lg|x1", status="research", research_job=10)
    es.update("excel|lg|x2", status="card", card_job=20)
    qdb = _queue_db(tmp_path, [(10, "pending"), (20, "processing")])

    cancel_fn = botrun.make_cancel_excel_fn(state_db, qdb)
    reply = cancel_fn("excel|lg|x1")

    assert "1" in reply
    assert [i.key for i in es.by_status("cancelled")] == ["excel|lg|x1"]
    assert _job_status(qdb, 10) == "cancelled"       # pending-задача агента снята
    assert [i.key for i in es.by_status("card")] == ["excel|lg|x2"]  # сосед не задет


def test_cancel_all_active_items(tmp_path):
    state_db = tmp_path / "state.db"
    es = ExcelStore(state_db)
    es.add_items([("excel|a|1", "A", "1", "Товар A1", 100),
                  ("excel|b|2", "B", "2", "Товар B2", 200),
                  ("excel|c|3", "C", "3", "Товар C3", 300)])
    es.update("excel|a|1", status="research", research_job=11)
    es.update("excel|b|2", status="card", card_job=12)
    es.update("excel|c|3", status="preview")          # уже доехал — не отменяется
    qdb = _queue_db(tmp_path, [(11, "pending"), (12, "pending")])

    cancel_fn = botrun.make_cancel_excel_fn(state_db, qdb)
    reply = cancel_fn("*")

    assert "2" in reply
    assert {i.key for i in es.by_status("cancelled")} == {"excel|a|1", "excel|b|2"}
    assert [i.key for i in es.by_status("preview")] == ["excel|c|3"]
    assert _job_status(qdb, 11) == "cancelled"
    assert _job_status(qdb, 12) == "cancelled"


def test_cancel_processing_job_not_touched_but_item_cancelled(tmp_path):
    # агент уже генерит — задачу в очереди не дёргаем (агент завершит вхолостую),
    # но товар из конвейера снят: тик его больше не продвинет
    state_db = tmp_path / "state.db"
    es = ExcelStore(state_db)
    es.add_items([("excel|d|4", "D", "4", "Товар D4", 400)])
    es.update("excel|d|4", status="card", card_job=30)
    qdb = _queue_db(tmp_path, [(30, "processing")])

    cancel_fn = botrun.make_cancel_excel_fn(state_db, qdb)
    cancel_fn("excel|d|4")

    assert [i.key for i in es.by_status("cancelled")] == ["excel|d|4"]
    assert _job_status(qdb, 30) == "processing"       # не трогаем — агент в работе


def test_cancel_nothing_active(tmp_path):
    state_db = tmp_path / "state.db"
    ExcelStore(state_db)                              # пустой конвейер
    qdb = _queue_db(tmp_path, [])
    cancel_fn = botrun.make_cancel_excel_fn(state_db, qdb)
    assert "❌" in cancel_fn("*")


def test_excel_cancel_markup_lists_active_items(tmp_path):
    # кнопки отмены под /excel: по одной на активный товар + «отменить все»
    from content_factory.publish.orders import OrderLinks
    state_db = tmp_path / "state.db"
    es = ExcelStore(state_db)
    es.add_items([("excel|lg|x1", "LG", "X1", "Холодильник LG X1", 100)])
    es.update("excel|lg|x1", status="research", research_job=10)
    links = OrderLinks(state_db)

    markup = botrun.excel_cancel_markup(state_db, links)

    flat = [b for row in markup["inline_keyboard"] for b in row]
    assert any(b["callback_data"].startswith("excancel:") for b in flat)
    assert any(b["callback_data"] == "excancel:*" for b in flat)


def test_excel_cancel_markup_empty_when_idle(tmp_path):
    from content_factory.publish.orders import OrderLinks
    state_db = tmp_path / "state.db"
    ExcelStore(state_db)
    links = OrderLinks(state_db)
    assert botrun.excel_cancel_markup(state_db, links) is None


# ── /sources: источники с наценками (2026-07-07) ─────────────────────────────
def test_sources_fn_lists_slots_with_markup(tmp_path):
    import openpyxl
    from content_factory.ingest.excel_price import set_markup
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["№", "Артикул", "Бренд", "Наименование", "Цена (руб.)", "Заказ (шт.)"])
    ws.append(["Чайники", "", "", "", "", ""])
    ws.append(["1", "10", "Vitek", "Чайник Vitek V1", "1000", ""])
    prices = tmp_path / "prices"
    prices.mkdir()
    wb.save(prices / "manual__ivanov.xlsx")
    set_markup(prices, "manual__ivanov", 5)

    text = botrun.make_sources_fn(prices)()
    assert "manual__ivanov" in text
    assert "+5%" in text
    assert "1 поз" in text


def test_markup_fn_sets_and_reports(tmp_path):
    prices = tmp_path / "prices"
    prices.mkdir()
    (prices / "manual__x.xlsx").write_bytes(b"PK")     # файл существует (не парсим)
    markup_fn = botrun.make_markup_fn(prices)
    reply = markup_fn("manual__x", -7)
    assert "-7" in reply
    from content_factory.ingest.excel_price import get_markups
    assert get_markups(prices) == {"manual__x": -7}
    assert "❌" in markup_fn("manual__нет_такого", 5)   # незнакомый слот