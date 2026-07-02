from urllib.parse import unquote_plus
import httpx
from content_factory.publish.telegram import PublishState, PublishResult
from content_factory.orchestrator.confirm_store import Awaiting
from content_factory.bot import run as botrun


def test_get_updates_parses_result():
    def handler(req):
        assert req.url.path == "/botTOK/getUpdates"
        return httpx.Response(200, json={"ok": True, "result": [{"update_id": 5}]})
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.telegram.org")
    upd = botrun.get_updates("TOK", 0, timeout=0, http=http)
    assert upd == [{"update_id": 5}]


def test_finalize_preview_replaces_buttons_with_verdict():
    reqs = []

    def handler(req):
        reqs.append((req.url.path, req.read()))
        return httpx.Response(200, json={"ok": True})
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.telegram.org")
    cq = {"message": {"chat": {"id": -100777}, "message_id": 42}}
    botrun.finalize_preview(http, "TOK", cq, "✅ опубликовано: k1")
    path, body = reqs[0]
    assert path == "/botTOK/editMessageReplyMarkup"
    decoded = unquote_plus(body.decode())          # form-URL-encoded → текст
    assert "message_id=42" in decoded and "✅ опубликовано: k1" in decoded


def test_finalize_preview_no_message_is_noop():
    def handler(req):
        raise AssertionError("не должно быть запросов")
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.telegram.org")
    botrun.finalize_preview(http, "TOK", {}, "✅")   # без message — тихо выходим


def test_make_publish_fn_uses_awaiting_channel(tmp_path):
    captured = {}

    def handler(req):
        captured["path"] = req.url.path
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.telegram.org")
    ps = PublishState(tmp_path / "p.db")
    fn = botrun.make_publish_fn("TOK", "HTML", ps, http=http)
    res = fn(Awaiting(key="k1", channel="@chan", card_path="https://x/c.jpg",
                      caption="cap", status="pending"))
    assert res.ok and res.message_id == 7
    assert captured["path"] == "/botTOK/sendPhoto"


def test_make_regen_fn_removes_card_and_store_entry(tmp_path):
    import sqlite3
    card = tmp_path / "NC_123.jpg"
    card.write_bytes(b"IMG")
    store_db = tmp_path / "cards.db"
    with sqlite3.connect(store_db) as c:
        c.execute("CREATE TABLE card_jobs (key TEXT PRIMARY KEY, input_filename TEXT, "
                  "status TEXT, tries INTEGER DEFAULT 0)")
        c.execute("INSERT INTO card_jobs VALUES ('NC_123', 'in.jpg', 'done', 1)")
    fn = botrun.make_regen_fn(store_db)
    a = Awaiting(key="breeze|x|y", channel="@c", card_path=str(card),
                 caption="cap", status="published")
    assert fn(a) is True
    assert not card.exists()                       # файл карточки удалён
    with sqlite3.connect(store_db) as c:
        assert c.execute("SELECT count(*) FROM card_jobs WHERE key='NC_123'").fetchone()[0] == 0


def test_make_regen_fn_survives_missing_file(tmp_path):
    import sqlite3
    store_db = tmp_path / "cards.db"
    with sqlite3.connect(store_db) as c:
        c.execute("CREATE TABLE card_jobs (key TEXT PRIMARY KEY, input_filename TEXT, "
                  "status TEXT, tries INTEGER DEFAULT 0)")
    fn = botrun.make_regen_fn(store_db)
    a = Awaiting(key="k", channel="@c", card_path=str(tmp_path / "нет_файла.jpg"),
                 caption="cap", status="pending")
    assert fn(a) is True                           # отсутствие файла/записи — не ошибка
