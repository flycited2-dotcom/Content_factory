"""Отправка карточки в очередь фотоагента (submit-job) — переиспользуемая фабрика
между periodic-тиком excel_run и визардом /task (план 2026-07-04-bot-task-wizard)."""
import sqlite3
import httpx
from content_factory.orchestrator.card_submit import make_card_submitter, slug


def _queue_db(tmp_path, input_filename="ext_1.jpg"):
    db = tmp_path / "q.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, input_filename TEXT, "
               "result_sent INTEGER DEFAULT 0)")
    con.execute("INSERT INTO jobs(input_filename) VALUES (?)", (input_filename,))
    con.commit()
    con.close()
    return str(db)


def test_slug_normalizes():
    assert slug("EXPERTAIR by ZILON") == "expertair-by-zilon"
    assert slug("") == ""


def test_submit_card_posts_and_silences(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "photo.png").write_bytes(b"IMG")
    db = _queue_db(tmp_path, input_filename="ext_new.jpg")
    captured = {}

    def handler(req):
        captured["path"] = req.url.path
        captured["body"] = req.read()
        return httpx.Response(200, json={"queued": "ext_new.jpg"})
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://x")
    submit_card = make_card_submitter("http://x", {"x-agent-token": "t"}, out, "123",
                                      db, http=http)
    job_id = submit_card("Ballu", "Olympio", "specs text", "photo.png")

    assert job_id == 1
    assert captured["path"] == "/api/submit-job"
    body = captured["body"].decode(errors="ignore")
    assert "Olympio" in body and "specs text" in body

    con = sqlite3.connect(db)
    assert con.execute("SELECT result_sent FROM jobs WHERE id=1").fetchone()[0] == 1
    con.close()


def test_submit_card_accepts_absolute_photo_path(tmp_path):
    photo = tmp_path / "manual.png"
    photo.write_bytes(b"PHOTO")
    out = tmp_path / "out"
    out.mkdir()                        # пустой output_dir — фото не оттуда, путь абсолютный
    db = _queue_db(tmp_path, input_filename="ext_abs.jpg")

    def handler(req):
        return httpx.Response(200, json={"queued": "ext_abs.jpg"})
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://x")
    submit_card = make_card_submitter("http://x", {}, out, "0", db, http=http)
    job_id = submit_card("Beko", "X100", "", str(photo))

    assert job_id == 1


def test_submit_card_raises_on_http_error(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "photo.png").write_bytes(b"IMG")
    db = _queue_db(tmp_path)

    def handler(req):
        return httpx.Response(500, text="boom")
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://x")
    submit_card = make_card_submitter("http://x", {}, out, "0", db, http=http)
    try:
        submit_card("Beko", "X100", "", "photo.png")
        assert False, "должно было упасть на raise_for_status"
    except httpx.HTTPStatusError:
        pass
