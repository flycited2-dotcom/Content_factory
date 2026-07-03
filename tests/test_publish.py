from urllib.parse import parse_qs
import httpx
from content_factory.publish.telegram import (
    publish_post, PublishState, send_message, edit_caption)


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler),
                        base_url="https://api.telegram.org")


def _form(req):
    """Распарсить x-www-form-urlencoded тело запроса (когда photo передаётся URL-ом)."""
    return {k: v[0] for k, v in parse_qs(req.content.decode("utf-8")).items()}


def test_sendphoto_by_url_ok():
    captured = {}

    def handler(req):
        captured["path"] = req.url.path
        captured["form"] = _form(req)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    res = publish_post("TOK", "@chan", "https://x/static/card.jpg", "Подпись",
                       http=_client(handler), parse_mode="HTML")
    assert res.ok and res.message_id == 42 and not res.skipped
    assert captured["path"] == "/botTOK/sendPhoto"
    assert captured["form"]["chat_id"] == "@chan"
    assert captured["form"]["caption"] == "Подпись"
    assert captured["form"]["photo"] == "https://x/static/card.jpg"
    assert captured["form"]["parse_mode"] == "HTML"


def test_sendphoto_local_file_multipart(tmp_path):
    card = tmp_path / "card.jpg"
    card.write_bytes(b"JPEGDATA")
    captured = {}

    def handler(req):
        captured["ctype"] = req.headers.get("content-type", "")
        captured["body"] = req.content
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    res = publish_post("TOK", "-100123", str(card), "Локальный файл", http=_client(handler))
    assert res.ok and res.message_id == 7
    assert "multipart/form-data" in captured["ctype"]
    assert b"JPEGDATA" in captured["body"]


def test_caption_truncated_to_limit():
    captured = {}

    def handler(req):
        captured["form"] = _form(req)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    publish_post("TOK", "@chan", "https://x/c.jpg", "Я" * 2000, http=_client(handler))
    assert len(captured["form"]["caption"]) <= 1024


def test_idempotent_skip_does_not_resend(tmp_path):
    state = PublishState(tmp_path / "pub.db")
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})

    r1 = publish_post("TOK", "@chan", "https://x/c.jpg", "txt",
                      http=_client(handler), key="breeze:NC1", state=state)
    r2 = publish_post("TOK", "@chan", "https://x/c.jpg", "txt",
                      http=_client(handler), key="breeze:NC1", state=state)
    assert r1.ok and not r1.skipped
    assert r2.ok and r2.skipped            # второй раз — пропуск
    assert calls["n"] == 1                  # http вызван только один раз


def test_tg_error_returns_held_and_not_marked(tmp_path):
    state = PublishState(tmp_path / "pub.db")

    def handler(req):
        return httpx.Response(200, json={"ok": False, "description": "chat not found"})

    res = publish_post("TOK", "@chan", "https://x/c.jpg", "txt",
                       http=_client(handler), key="breeze:NC1", state=state)
    assert not res.ok and "chat not found" in (res.error or "")
    assert not state.is_published("breeze:NC1")   # ошибку не считаем опубликованной


def test_sendphoto_with_reply_markup():
    captured = {}

    def handler(req):
        captured["form"] = _form(req)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})

    rm = '{"inline_keyboard":[[{"text":"OK","callback_data":"approve:k1"}]]}'
    res = publish_post("TOK", "@chan", "https://x/c.jpg", "txt", http=_client(handler),
                       reply_markup=rm)
    assert res.ok
    assert captured["form"]["reply_markup"] == rm


def test_send_message_ok():
    captured = {}

    def handler(req):
        captured["path"] = req.url.path
        captured["form"] = _form(req)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 3}})

    ok = send_message("TOK", "123", "алерт", http=_client(handler))
    assert ok is True
    assert captured["path"] == "/botTOK/sendMessage"
    assert captured["form"]["chat_id"] == "123"
    assert captured["form"]["text"] == "алерт"


def test_published_keys_returns_all(tmp_path):
    st = PublishState(tmp_path / "p.db")
    st.mark("k1", 1)
    st.mark("k2", 2)
    assert st.published_keys() == {"k1", "k2"}


def test_retry_then_success():
    state_calls = {"n": 0}

    def handler(req):
        state_calls["n"] += 1
        if state_calls["n"] == 1:
            raise httpx.ConnectError("boom")           # первая попытка — сетевой сбой
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})

    res = publish_post("TOK", "@chan", "https://x/c.jpg", "txt",
                       http=_client(handler), retries=2, backoff=0)
    assert res.ok and res.message_id == 9 and state_calls["n"] == 2


# ── «живой канал»: расширение PublishState (channel/price/status/caption) ─────
def test_publish_state_migration_and_records(tmp_path):
    ps = PublishState(tmp_path / "s.db")
    ps.mark("k1", 10, channel="@chan", caption="cap1")
    recs = ps.records()
    assert [(r.key, r.message_id, r.channel, r.caption, r.status, r.price)
            for r in recs] == [("k1", 10, "@chan", "cap1", "active", None)]


def test_publish_state_update_sync(tmp_path):
    ps = PublishState(tmp_path / "s.db")
    ps.mark("k1", 10, channel="@chan", caption="cap1")
    ps.update_sync("k1", status="sold")
    ps.update_sync("k1", price=19990, caption="cap2")
    (r,) = ps.records()
    assert (r.status, r.price, r.caption) == ("sold", 19990, "cap2")


def test_publish_state_mark_backcompat(tmp_path):
    ps = PublishState(tmp_path / "s.db")
    ps.mark("k0", 5)                       # старый вызов без channel/caption
    (r,) = ps.records()
    assert (r.channel, r.caption, r.status) == ("", None, "active")


# ── edit_caption («живой канал» правит посты) ─────────────────────────────────
def test_edit_caption_ok():
    reqs = []

    def handler(req):
        reqs.append((req.url.path, req.read()))
        return httpx.Response(200, json={"ok": True})
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.telegram.org")
    ok, err, gone = edit_caption("TOK", "@chan", 42, "новая подпись", http=http)
    assert (ok, err, gone) == (True, None, False)
    path, body = reqs[0]
    assert path == "/botTOK/editMessageCaption"
    from urllib.parse import unquote_plus
    decoded = unquote_plus(body.decode())
    assert "message_id=42" in decoded and "новая подпись" in decoded


def test_edit_caption_message_gone():
    def handler(req):
        return httpx.Response(400, json={"ok": False,
                                         "description": "Bad Request: message to edit not found"})
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.telegram.org")
    ok, err, gone = edit_caption("TOK", "@chan", 42, "cap", http=http)
    assert not ok and gone                        # пост удалён руками → больше не трогаем


def test_edit_caption_transient_retries():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(500)
        return httpx.Response(200, json={"ok": True})
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.telegram.org")
    ok, err, gone = edit_caption("TOK", "@chan", 42, "cap", http=http, retries=1, backoff=0)
    assert ok and len(calls) == 2
