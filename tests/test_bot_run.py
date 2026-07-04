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


def test_publish_fn_adds_order_button(tmp_path):
    from content_factory.publish.orders import OrderLinks
    captured = {}

    def handler(req):
        captured["body"] = req.read()
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.telegram.org")
    ps = PublishState(tmp_path / "p.db")
    links = OrderLinks(tmp_path / "p.db")
    fn = botrun.make_publish_fn("TOK", "HTML", ps, http=http,
                                order_bot="Sendpr1ce_bot", links=links)
    res = fn(Awaiting(key="k1", channel="@chan", card_path="https://x/c.jpg",
                      caption="cap", status="pending"))
    assert res.ok
    decoded = unquote_plus(captured["body"].decode())
    assert "t.me/Sendpr1ce_bot?start=ord_" in decoded      # кнопка «Заказать» в посте


def test_make_fn_selects_and_stores(tmp_path):
    import openpyxl
    from content_factory.orchestrator.excel_pipeline import ExcelStore
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["№", "Артикул", "Бренд", "Наименование", "Цена (руб.)", "Заказ (шт.)"])
    ws.append(["Холодильники", "", "", "", "", ""])
    ws.append(["1", "10", "Beko", "Холодильник Beko X100", "30000", ""])
    ws.append(["2", "11", "Candy", "Холодильник Candy Y200", "25000", ""])
    prices = tmp_path / "prices"
    prices.mkdir()
    wb.save(prices / "manual.xlsx")
    fn = botrun.make_make_fn(tmp_path / "state.db", prices)
    reply = fn(2, "холодильники", {})
    assert "✅ выбрано 2" in reply and "Beko X100" in reply
    items = ExcelStore(tmp_path / "state.db").by_status("new")
    assert {i.key for i in items} == {"excel|beko|x100", "excel|candy|y200"}
    reply2 = fn(2, "холодильники", {})                    # повтор — всё уже в работе
    assert "❌" in reply2


def test_make_fn_searches_both_manual_and_mail_slots(tmp_path):
    # грабля 2026-07-03: почта (cf-mail) молча затирала единственный latest.xlsx —
    # позиции владельца из его собственного прайса переставали находиться.
    import openpyxl
    from content_factory.orchestrator.excel_pipeline import ExcelStore
    prices = tmp_path / "prices"
    prices.mkdir()

    wb1 = openpyxl.Workbook(); ws1 = wb1.active
    ws1.append(["№", "Артикул", "Бренд", "Наименование", "Цена (руб.)", "Заказ (шт.)"])
    ws1.append(["Холодильники", "", "", "", "", ""])
    ws1.append(["1", "10", "Beko", "Холодильник Beko X100", "30000", ""])
    wb1.save(prices / "manual.xlsx")

    wb2 = openpyxl.Workbook(); ws2 = wb2.active
    ws2.append(["№", "Артикул", "Бренд", "Наименование", "Цена (руб.)", "Заказ (шт.)"])
    ws2.append(["Холодильники", "", "", "", "", ""])
    ws2.append(["2", "11", "Candy", "Холодильник Candy Y200", "25000", ""])
    wb2.save(prices / "mail.xlsx")

    fn = botrun.make_make_fn(tmp_path / "state.db", prices)
    reply = fn(2, "холодильники", {})
    assert "Beko X100" in reply and "Candy Y200" in reply     # найдено в обоих слотах
    items = ExcelStore(tmp_path / "state.db").by_status("new")
    assert {i.key for i in items} == {"excel|beko|x100", "excel|candy|y200"}


# ── download_telegram_file: общий хелпер getFile → байты (визард /task, шаг 4) ──
def test_download_telegram_file_success():
    def handler(req):
        if req.url.path == "/botTOK/getFile":
            return httpx.Response(200, json={"ok": True,
                                             "result": {"file_path": "photos/file_1.jpg"}})
        if req.url.path == "/file/botTOK/photos/file_1.jpg":
            return httpx.Response(200, content=b"IMGBYTES")
        raise AssertionError(f"неожиданный путь: {req.url.path}")
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.telegram.org")
    data = botrun.download_telegram_file(http, "TOK", "fid123")
    assert data == b"IMGBYTES"


def test_download_telegram_file_missing_path_returns_none():
    def handler(req):
        return httpx.Response(200, json={"ok": True, "result": {}})
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.telegram.org")
    assert botrun.download_telegram_file(http, "TOK", "fid123") is None


def test_receive_price_saves_to_manual_slot(tmp_path):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["№", "Артикул", "Бренд", "Наименование", "Цена (руб.)", "Заказ (шт.)"])
    ws.append(["Холодильники", "", "", "", "", ""])
    ws.append(["1", "10", "Beko", "Холодильник Beko X100", "30000", ""])
    import io
    buf = io.BytesIO()
    wb.save(buf)
    xlsx_bytes = buf.getvalue()

    def handler(req):
        if req.url.path == "/botTOK/getFile":
            return httpx.Response(200, json={"ok": True,
                                             "result": {"file_path": "documents/f.xlsx"}})
        if req.url.path == "/file/botTOK/documents/f.xlsx":
            return httpx.Response(200, content=xlsx_bytes)
        raise AssertionError(f"неожиданный путь: {req.url.path}")
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.telegram.org")
    reply = botrun.receive_price(http, "TOK", {"file_id": "fid", "file_name": "price.xlsx"},
                                 tmp_path)

    assert "1 позиций" in reply and "Холодильники" in reply
    assert (tmp_path / "manual.xlsx").read_bytes() == xlsx_bytes
    assert (tmp_path / "price.xlsx").read_bytes() == xlsx_bytes


def test_receive_price_download_failure():
    def handler(req):
        return httpx.Response(200, json={"ok": True, "result": {}})
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.telegram.org")
    reply = botrun.receive_price(http, "TOK", {"file_id": "fid"}, "unused")
    assert "❌" in reply


def test_receive_price_saves_to_named_slot(tmp_path):
    # источник-канал (2026-07-04): та же логика, другой слот (не перезаписывает
    # manual.xlsx владельца — load_price_slots ищет во всех трёх)
    import io
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["№", "Артикул", "Бренд", "Наименование", "Цена (руб.)", "Заказ (шт.)"])
    ws.append(["Телевизоры", "", "", "", "", ""])
    ws.append(["1", "20", "MIU", "Телевизор MIU H40", "20000", ""])
    buf = io.BytesIO()
    wb.save(buf)
    xlsx_bytes = buf.getvalue()

    def handler(req):
        if req.url.path == "/botTOK/getFile":
            return httpx.Response(200, json={"ok": True,
                                             "result": {"file_path": "documents/f.xlsx"}})
        if req.url.path == "/file/botTOK/documents/f.xlsx":
            return httpx.Response(200, content=xlsx_bytes)
        raise AssertionError(f"неожиданный путь: {req.url.path}")
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.telegram.org")
    reply = botrun.receive_price(http, "TOK", {"file_id": "fid", "file_name": "БытТехОпт.xlsx"},
                                 tmp_path, slot="channel")

    assert "1 позиций" in reply
    assert (tmp_path / "channel.xlsx").read_bytes() == xlsx_bytes
    assert not (tmp_path / "manual.xlsx").exists()   # свой прайс владельца не задет


def test_resolve_callback_data_expands_code(tmp_path):
    from content_factory.publish.orders import OrderLinks
    from content_factory.orchestrator.confirm_store import ConfirmStore
    cs = ConfirmStore(tmp_path / "s.db")
    links = OrderLinks(tmp_path / "s.db")
    long_key = "excel|генератор бензиновый carver ppg - 1200i cube инверторный"
    cs.add(long_key, "@chan", "/c/x.jpg", "cap")
    code = links.code_for(long_key)
    assert botrun.resolve_callback_data(f"approve:{code}", cs, links) == f"approve:{long_key}"
    # обычные короткие ключи проходят как есть
    cs.add("breeze|funai|daijin", "@chan", "/c/y.jpg", "cap")
    assert botrun.resolve_callback_data("approve:breeze|funai|daijin", cs, links) == \
        "approve:breeze|funai|daijin"
    assert botrun.resolve_callback_data("noop", cs, links) == "noop"
