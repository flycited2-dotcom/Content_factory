import sqlite3
from decimal import Decimal
import httpx
from content_factory.models import Offer
from content_factory.catalog.series import group_by_series
from content_factory.cards_pipeline import (
    FotogenConfig, submit_card_job, done_results, failed_inputs,
    CardJobStore, specs_text, run_once, wake_agent,
)


def test_wake_agent_sets_start_flag(tmp_path):
    db = str(tmp_path / "q.db")
    sqlite3.connect(db).close()
    wake_agent(db)
    con = sqlite3.connect(db)
    assert con.execute("SELECT value FROM flags WHERE key='agent_command'").fetchone()[0] == "start"
    con.close()


def _make_queue_db(tmp_path, rows):
    db = tmp_path / "queue.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, status TEXT, input_filename TEXT, output_filename TEXT)")
    for st, inf, outf in rows:
        con.execute("INSERT INTO jobs(status,input_filename,output_filename) VALUES(?,?,?)", (st, inf, outf))
    con.commit(); con.close()
    return str(db)


def _cfg(tmp_path, **kw):
    base = dict(api_url="http://x", token="t", chat_id=1,
                queue_db=str(tmp_path / "q.db"), output_dir=str(tmp_path / "out"),
                cards_dir=str(tmp_path / "cards"), per_run=8)
    base.update(kw)
    return FotogenConfig(**base)


def test_submit_card_job_returns_queued_name():
    def handler(req):
        assert req.url.path == "/api/submit-job"
        return httpx.Response(200, json={"ok": True, "queued": "ext_123.jpg"})
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://x")
    cfg = FotogenConfig(api_url="http://x", token="t", chat_id=1, queue_db="", output_dir="", cards_dir="")
    assert submit_card_job(cfg, b"img", "Ballu", "Olympio", "specs", http=http) == "ext_123.jpg"


def test_done_and_failed_queries(tmp_path):
    db = _make_queue_db(tmp_path, [("done", "a.jpg", "card_a.png"),
                                   ("failed", "b.jpg", None),
                                   ("processing", "c.jpg", None)])
    assert done_results(db, ["a.jpg", "b.jpg", "c.jpg"]) == {"a.jpg": "card_a.png"}
    assert failed_inputs(db, ["a.jpg", "b.jpg", "c.jpg"]) == {"b.jpg"}


def test_card_job_store(tmp_path):
    s = CardJobStore(tmp_path / "s.db")
    s.record("k1", "ext_1.jpg", "pending")
    assert s.get("k1") == ("ext_1.jpg", "pending", 0)
    assert s.pending() == [("k1", "ext_1.jpg")]
    s.record("k1", "ext_1.jpg", "done")
    assert s.pending() == []


def test_specs_text_filters():
    t = specs_text({"Бренд": "X", "Холод, кВт": "3.5", "Пусто": ""})
    assert "Холод, кВт: 3.5" in t and "Бренд" not in t and "Пусто" not in t


def _o(sku, btu, photo, series="Olympio"):
    return Offer(supplier_sku=sku, source="breeze", brand="Ballu", model=f"{series} {btu}",
                 category_id=2, btu_calc=btu, attrs={"Холод, кВт": "2.0"}, cost=Decimal("1"),
                 retail_ref=None, stock=1, photos=[photo] if photo else [], series=series,
                 content_hash="h")


def test_run_once_submits_and_publishes(tmp_path):
    # одна серия без карточки → submit; и одна готовая в очереди → publish
    out = tmp_path / "out"; out.mkdir()
    (out / "card_ready.png").write_bytes(b"READY")
    db = _make_queue_db(tmp_path, [("done", "ext_ready.jpg", "card_ready.png")])
    cfg = _cfg(tmp_path, queue_db=db, output_dir=str(out))
    store = CardJobStore(tmp_path / "s.db")
    from content_factory.content.cards import card_key
    store.record(card_key("breeze:NC1"), "ext_ready.jpg", "pending")   # ждёт готовую

    groups = group_by_series([_o("breeze:NC1", 7, "http://p/1.jpg", series="Olympio"),
                              _o("breeze:NC2", 9, "http://p/2.jpg", series="Gloria")])

    def handler(req):
        return httpx.Response(200, json={"queued": "ext_new.jpg"})
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://x")
    submitted, published = run_once(groups, cfg, store, http=http, fetch_photo=lambda u: b"img")

    assert published == 1                                  # готовая скопирована
    assert (tmp_path / "cards" / f"{card_key('breeze:NC1')}.jpg").read_bytes() == b"READY"
    assert submitted >= 1                                  # новая серия поставлена в очередь


def test_run_once_uses_per_series_mode(tmp_path):
    out = tmp_path / "out"; out.mkdir()
    cfg = _cfg(tmp_path, queue_db=_make_queue_db(tmp_path, []), output_dir=str(out),
               mode="conditioner", modes={"breeze|ballu|gloria": "mcp"})
    store = CardJobStore(tmp_path / "s.db")
    groups = group_by_series([_o("breeze:NC2", 9, "http://p/2.jpg", series="Gloria")])
    seen = {}

    def handler(req):
        seen["mcp"] = b"mcp" in req.content                # mode=mcp ушёл в форму
        return httpx.Response(200, json={"queued": "ext.jpg"})
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://x")
    run_once(groups, cfg, store, http=http, fetch_photo=lambda u: b"img")
    assert seen.get("mcp") is True


def test_run_once_uses_specs_fn(tmp_path):
    out = tmp_path / "out"; out.mkdir()
    cfg = _cfg(tmp_path, queue_db=_make_queue_db(tmp_path, []), output_dir=str(out))
    store = CardJobStore(tmp_path / "s.db")
    groups = group_by_series([_o("breeze:NC2", 9, "http://p/2.jpg", series="Gloria")])
    seen = {}

    def handler(req):
        seen["has"] = b"RICHSPECS" in req.content       # specs из specs_fn ушли агенту
        return httpx.Response(200, json={"queued": "ext.jpg"})
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://x")
    run_once(groups, cfg, store, http=http, fetch_photo=lambda u: b"img",
             specs_fn=lambda g: "RICHSPECS\nКлюч: значение")
    assert seen.get("has") is True


def test_run_once_retries_failed_until_cap(tmp_path):
    from content_factory.content.cards import card_key
    out = tmp_path / "out"; out.mkdir()
    cfg = _cfg(tmp_path, queue_db=_make_queue_db(tmp_path, []), output_dir=str(out))
    store = CardJobStore(tmp_path / "s.db")
    k = card_key("breeze:NC2")
    store.record(k, "old.jpg", "failed", tries=1)          # 1 неудачная попытка
    groups = group_by_series([_o("breeze:NC2", 9, "http://p/2.jpg", series="Gloria")])

    def handler(req):
        return httpx.Response(200, json={"queued": "ext_new.jpg"})
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://x")
    submitted, _ = run_once(groups, cfg, store, http=http, fetch_photo=lambda u: b"img")
    assert submitted == 1                                  # failed с запасом попыток → переотправлен
    assert store.get(k)[2] == 2                            # счётчик попыток вырос


def test_run_once_requeues_cancelled(tmp_path):
    # задачу сняли в очереди агента (cancelled) → store не держит её pending вечно,
    # серия переотправляется в том же прогоне (грабля 2026-07-03: снятые сплиты
    # съедали весь бюджет max_pending)
    from content_factory.content.cards import card_key
    out = tmp_path / "out"; out.mkdir()
    db = _make_queue_db(tmp_path, [("cancelled", "ext_old.jpg", None)])
    cfg = _cfg(tmp_path, queue_db=db, output_dir=str(out))
    store = CardJobStore(tmp_path / "s.db")
    k = card_key("breeze:NC2")
    store.record(k, "ext_old.jpg", "pending", tries=1)
    groups = group_by_series([_o("breeze:NC2", 9, "http://p/2.jpg", series="Gloria")])

    def handler(req):
        return httpx.Response(200, json={"queued": "ext_new.jpg"})
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://x")
    submitted, _ = run_once(groups, cfg, store, http=http, fetch_photo=lambda u: b"img")
    assert submitted == 1                                  # снятая задача переотправлена
    assert store.get(k) == ("ext_new.jpg", "pending", 2)   # новая попытка учтена


def test_run_once_gives_up_after_max_tries(tmp_path):
    from content_factory.content.cards import card_key
    from content_factory.cards_pipeline import MAX_TRIES
    out = tmp_path / "out"; out.mkdir()
    cfg = _cfg(tmp_path, queue_db=_make_queue_db(tmp_path, []), output_dir=str(out))
    store = CardJobStore(tmp_path / "s.db")
    k = card_key("breeze:NC2")
    store.record(k, "old.jpg", "failed", tries=MAX_TRIES)  # попытки исчерпаны
    groups = group_by_series([_o("breeze:NC2", 9, "http://p/2.jpg", series="Gloria")])

    def handler(req):
        return httpx.Response(200, json={"queued": "x"})
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://x")
    submitted, _ = run_once(groups, cfg, store, http=http, fetch_photo=lambda u: b"img")
    assert submitted == 0                                  # больше не долбим агента
