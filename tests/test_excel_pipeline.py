"""Конвейер excel-товара: new → research → card → preview (+ кэш УТП, ретраи)."""
from content_factory.orchestrator.excel_pipeline import ExcelStore, tick


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

    def submit_card(brand, model, utp, photo_path):
        calls["card"].append((brand, model, utp, photo_path))
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
    assert calls["card"] == [("Beko", "X1", "✓ Кэш УТП", "/photos/x1.png")]


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


def test_preview_caption_escapes_html():
    from content_factory.orchestrator.excel_run import build_preview_caption
    cap = build_preview_caption("Чайник Vitek VT-7032 <VT-7032 BN>", 995,
                                "✓ 1.7 л <стекло>")
    assert "<VT-7032" not in cap and "&lt;VT-7032 BN&gt;" in cap
    assert "💰 995 ₽" in cap
    assert "&lt;стекло&gt;" in cap
