"""Конвейер excel-товара: new → research → card → preview (+ кэш УТП, ретраи)."""
import time as _time

from content_factory.orchestrator.excel_pipeline import (ExcelStore, parse_research_result,
                                                         tick)


def test_add_items_with_due_at_hidden_until_due(tmp_path):
    # /task с расписанием (2026-07-07): товар с due_at в будущем не берётся тиком
    es = ExcelStore(tmp_path / "s.db")
    es.add_items([("excel|a|1", "A", "1", "Товар A1", 100)],
                 due_at=_time.time() + 3600)                 # через час
    es.add_items([("excel|b|2", "B", "2", "Товар B2", 200)])  # без расписания
    assert [i.key for i in es.by_status("new")] == ["excel|b|2"]  # будущий скрыт


def test_due_item_appears_when_time_comes(tmp_path):
    es = ExcelStore(tmp_path / "s.db")
    es.add_items([("excel|a|1", "A", "1", "Товар A1", 100)],
                 due_at=_time.time() - 10)                   # срок уже наступил
    assert [i.key for i in es.by_status("new")] == ["excel|a|1"]


def test_due_at_survives_existing_db_migration(tmp_path):
    # грабля SQLite: колонку в существующую таблицу — только идемпотентный ALTER
    import sqlite3
    db = tmp_path / "s.db"
    with sqlite3.connect(db) as c:                           # старая схема без due_at
        c.execute("CREATE TABLE excel_items ("
                  "key TEXT PRIMARY KEY, brand TEXT, model TEXT, name TEXT, "
                  "price INTEGER, status TEXT DEFAULT 'new', research_job INTEGER, "
                  "card_job INTEGER, tries INTEGER DEFAULT 0, error TEXT, ts REAL)")
        c.execute("INSERT INTO excel_items(key, name, status, ts) "
                  "VALUES('excel|old|x', 'Старый товар', 'new', 1)")
    es = ExcelStore(db)                                      # миграция в __init__
    assert [i.key for i in es.by_status("new")] == ["excel|old|x"]  # старые видны


def _store(tmp_path):
    s = ExcelStore(tmp_path / "s.db")
    s.add_items([("excel|beko|x1", "Beko", "X1", "Холодильник Beko X1", 30000)])
    return s


def _fns(jobs=None, research_id=101, card_id=201):
    """Фейки: submit_research/submit_card пишут вызовы, read_job отдаёт из jobs."""
    calls = {"research": [], "card": [], "preview": []}

    def submit_research(brand, model, category):
        calls["research"].append((brand, model, category))
        return research_id

    def read_job(job_id):
        return (jobs or {}).get(job_id, ("pending", None, None, None))

    def submit_card(brand, model, utp, photo_path, mode="kbt"):
        calls["card"].append((brand, model, utp, photo_path, mode))
        return card_id

    def preview(item, card_file):
        calls["preview"].append((item.key, card_file))
        return True
    return calls, submit_research, read_job, submit_card, preview


def test_new_goes_to_research(tmp_path):
    s = _store(tmp_path)
    calls, *fns = _fns()
    tick(s, *fns)
    (item,) = s.by_status("research")
    assert item.research_job == 101
    assert calls["research"] == [("Beko", "X1", "Холодильник")]


def test_cache_hit_skips_research(tmp_path):
    s = _store(tmp_path)
    s.cache_put("beko|x1", "✓ Кэш УТП", "/photos/x1.png")
    calls, *fns = _fns()
    tick(s, *fns)
    (item,) = s.by_status("card")
    assert calls["research"] == []                        # ChatGPT не тронут
    assert calls["card"] == [("Beko", "X1", "✓ Кэш УТП", "/photos/x1.png", "kbt")]


def test_research_done_caches_and_submits_card(tmp_path):
    s = _store(tmp_path)
    calls, *fns = _fns(jobs={101: ("done", "research_101.png", "✓ УТП", None)})
    tick(s, *fns)                                          # new → research
    tick(s, *fns)                                          # research done → card
    (item,) = s.by_status("card")
    assert item.card_job == 201
    assert calls["card"][0][2] == "✓ УТП"
    assert s.cache_get("beko|x1")[0] == "✓ УТП"            # кэш пополнен


def test_card_done_previews(tmp_path):
    s = _store(tmp_path)
    jobs = {101: ("done", "research_101.png", "✓ УТП", None),
            201: ("done", "beko_x1.png", None, None)}
    calls, *fns = _fns(jobs=jobs)
    for _ in range(3):
        tick(s, *fns)
    (item,) = s.by_status("preview")
    assert calls["preview"] == [("excel|beko|x1", "beko_x1.png")]


def test_research_failed_retries_then_fails(tmp_path):
    s = _store(tmp_path)
    jobs = {101: ("failed", None, None, "boom")}
    calls, *fns = _fns(jobs=jobs)
    tick(s, *fns)                                          # new → research
    tick(s, *fns)                                          # failed → ретрай (research заново)
    assert len(calls["research"]) == 2
    tick(s, *fns)                                          # failed повторно → failed навсегда
    (item,) = s.by_status("failed")
    assert "boom" in (item.error or "")


def test_research_done_without_photo_fails(tmp_path):
    s = _store(tmp_path)
    jobs = {101: ("done", None, "✓ УТП", None)}            # фото не нашлось
    calls, *fns = _fns(jobs=jobs)
    tick(s, *fns)
    tick(s, *fns)
    (item,) = s.by_status("failed")
    assert "фото" in (item.error or "").lower()
    assert calls["card"] == []


def test_retry_failed_returns_items_to_pipeline(tmp_path):
    # запрос владельца 2026-07-07: failed-позиции («research без фото»,
    # таймауты) возвращать в конвейер одной командой, а не терять
    s = _store(tmp_path)
    s.update("excel|beko|x1", status="failed", error="research без фото", tries=2)
    n = s.retry_failed()
    assert n == 1
    (item,) = s.by_status("new")
    assert item.tries == 0 and item.error is None       # чистый повторный заход
    assert s.retry_failed() == 0                        # повторно нечего


def test_preview_caption_escapes_html():
    from content_factory.orchestrator.excel_run import build_preview_caption
    cap = build_preview_caption("Чайник Vitek VT-7032 <VT-7032 BN>", 995,
                                "✓ 1.7 л <стекло>")
    assert "<VT-7032" not in cap and "&lt;VT-7032 BN&gt;" in cap
    assert "<blockquote>💎 <b>995 ₽</b></blockquote>" in cap
    assert "&lt;стекло&gt;" in cap


# ── видимость расписания (жалоба 2026-07-10: «полный вакуум информации» —
# запланированные /task-позиции невидимы в /excel, by_status('new') их прячет) ──
def test_scheduled_returns_future_items_sorted(tmp_path):
    from content_factory.orchestrator.excel_pipeline import ExcelStore
    es = ExcelStore(tmp_path / "s.db")
    es.add_items([("excel|a|1", "A", "1", "Товар A1", 100)], due_at=2000.0)
    es.add_items([("excel|b|2", "B", "2", "Товар B2", 200)], due_at=1500.0)
    es.add_items([("excel|c|3", "C", "3", "Товар C3", 300)])          # без расписания
    sched = es.scheduled(now=1000.0)
    assert [s["brand"] for s in sched] == ["B", "A"]                  # по due_at
    assert sched[0]["due_at"] == 1500.0
    assert es.scheduled(now=2500.0) == []                             # все дозрели


def test_due_scheduled_returns_matured_only(tmp_path):
    from content_factory.orchestrator.excel_pipeline import ExcelStore
    es = ExcelStore(tmp_path / "s.db")
    es.add_items([("excel|a|1", "A", "1", "Товар A1", 100)], due_at=2000.0)
    es.add_items([("excel|b|2", "B", "2", "Товар B2", 200)], due_at=1500.0)
    es.add_items([("excel|c|3", "C", "3", "Товар C3", 300)])          # без расписания
    due = es.due_scheduled(now=1600.0)
    assert [d["brand"] for d in due] == ["B"]                         # дозрел только B
    assert due[0]["key"] == "excel|b|2"
    es.update("excel|b|2", status="research")                         # тик забрал
    assert es.due_scheduled(now=1600.0) == []                         # алерт одноразовый


def test_telegram_start_alert_only_for_manual_tasks_that_can_start(tmp_path):
    from content_factory.orchestrator.excel_run import telegram_starting_items
    from content_factory.orchestrator.excel_pipeline import ExcelStore
    es = ExcelStore(tmp_path / "s.db")
    es.add_items([("excel|a|1", "A", "1", "Товар A1", 100)], due_at=1.0)
    es.add_items([("ready-price|2", "B", "2", "Товар B2", 200)], due_at=1.0)
    assert telegram_starting_items(es, False) == []
    assert [x["key"] for x in telegram_starting_items(es, True)] == ["excel|a|1"]


def test_verified_research_json_keeps_evidence():
    raw = '{"exact_model":true,"source_url":"https://maker.test/x1","photo_url":"https://maker.test/x1.png","features":["No Frost","320 л","39 дБ"]}'
    utp, evidence = parse_research_result(raw)
    assert utp == "✓ No Frost\n✓ 320 л\n✓ 39 дБ"
    assert evidence["source_url"] == "https://maker.test/x1"


def test_ready_price_ignores_unverified_legacy_cache(tmp_path):
    s = ExcelStore(tmp_path / "s.db")
    s.add_items([("ready-price|A-1", "Beko", "X1", "Холодильник Beko X1", 30000,
                  "mcp")])
    s.cache_put("beko|x1", "✓ старый кэш", "research_old.png")
    calls, *fns = _fns()
    tick(s, *fns)
    assert calls["card"] == []
    assert calls["research"] == [("Beko", "X1", "Холодильник")]


def test_ready_price_uses_requested_card_mode(tmp_path):
    s = ExcelStore(tmp_path / "s.db")
    s.add_items([("ready-price|TV-1", "BQ", "43F34B", "Телевизор BQ 43F34B", 20000,
                  "kbt")])
    evidence = {"exact_model": True, "source_url": "https://bq.test/43f34b",
                "photo_url": "https://bq.test/43f34b.png", "features": ["4K", "HDR", "Wi-Fi"]}
    s.cache_put("bq|43f34b", "✓ 4K", "research_tv.png", evidence=evidence)
    calls, *fns = _fns()
    tick(s, *fns)
    assert calls["card"][0][-1] == "kbt"


def test_ready_price_card_is_saved_for_avito_without_telegram(tmp_path):
    from content_factory.orchestrator.excel_run import save_ready_price_content
    s = ExcelStore(tmp_path / "state.db")
    s.add_items([("ready-price|A-1", "BQ", "KT100", "Kettle BQ KT100", 1050,
                  "mcp")])
    evidence = {"exact_model": True, "source_url": "https://bq.test/kt100",
                "photo_url": "https://bq.test/kt100.png", "features": ["1.7 L"]}
    s.cache_put("bq|kt100", "1.7 L", "research.png", evidence=evidence)
    output = tmp_path / "output"; output.mkdir()
    (output / "card.png").write_bytes(b"card")
    (output / "research.png").write_bytes(b"original")
    item = s.get("ready-price|A-1")
    from unittest.mock import patch
    with patch("content_factory.orchestrator.excel_run.audit_card_text",
               return_value={"passed": True, "engine": "test", "unverified_terms": []}):
        assert save_ready_price_content(s, item, "card.png", output,
                                        tmp_path / "ready") == (True, None)
    manifest = __import__("json").loads((tmp_path / "ready/A-1/content.json").read_text())
    assert manifest["article"] == "A-1" and manifest["evidence"] == evidence
    assert manifest["original"] == "original.png"
    assert manifest["card_text_audit"]["passed"] is True
    assert (tmp_path / "ready/A-1/card.png").read_bytes() == b"card"
    assert (tmp_path / "ready/A-1/original.png").read_bytes() == b"original"
