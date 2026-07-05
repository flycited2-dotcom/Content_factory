"""Опросник заказа клиента (задача 3 + follow-up): старт → кол-во → комментарий
→ телефон (request_contact) → лид (товар/цена/кол-во/телефон/комментарий/клиент)."""
from content_factory.publish.orders import OrderLinks
from content_factory.publish.telegram import PublishState
from content_factory.bot.order_dialog import OrderDialogStore
from content_factory.bot.order_flow import make_order_flow

USER = {"id": 555, "username": "ivan", "first_name": "Иван"}
CAPTION = ("Утюг Blackton SI1113 2200Вт, керамика\n"
           "<blockquote>💎 <b>2 190 ₽</b></blockquote>\n═══\nещё текст")


def _setup(tmp_path):
    db = tmp_path / "s.db"
    links = OrderLinks(db)
    ps = PublishState(db)
    store = OrderDialogStore(db)
    key = "excel|blackton|si1113"
    ps.mark(key, 1, channel="@chan", caption=CAPTION)
    code = links.code_for(key)
    start, callback, text, contact = make_order_flow(store, links, ps)
    return links, store, key, code, start, callback, text, contact


def test_start_shows_item_and_qty_buttons(tmp_path):
    links, store, key, code, start, callback, text, contact = _setup(tmp_path)
    r = start("777", code, USER)
    assert "Blackton SI1113" in r.text and "2 190" in r.text
    assert r.lead is None
    buttons = [b["callback_data"] for row in r.markup["inline_keyboard"] for b in row]
    assert "order:qty:1" in buttons and "order:qty:custom" in buttons
    assert store.snapshot("777").step == "awaiting_qty"


def test_start_unknown_code_no_dialog(tmp_path):
    links, store, key, code, start, callback, text, contact = _setup(tmp_path)
    r = start("777", "zzz", USER)
    assert "не найден" in r.text.lower() and r.markup is None
    assert store.snapshot("777") is None


def test_qty_then_comment_then_phone_contact(tmp_path):
    links, store, key, code, start, callback, text, contact = _setup(tmp_path)
    start("777", code, USER)
    r2 = callback("777", "order:qty:2", USER)
    assert "омментар" in r2.text                            # приглашение к комментарию
    r3 = text("777", "нужна доставка в Симферополь", USER)
    assert "елефон" in r3.text and r3.keyboard is not None  # приглашение к телефону
    assert store.snapshot("777").step == "awaiting_phone"
    # клиент поделился контактом
    r4 = contact("777", "+79781234567", USER)
    assert r4.lead is not None
    assert "Утюг Blackton SI1113" in r4.lead                # наименование
    assert "2 190" in r4.lead                               # цена
    assert "Количество: 2 шт." in r4.lead
    assert "Телефон: +79781234567" in r4.lead
    assert "Комментарий: нужна доставка в Симферополь" in r4.lead
    assert "@ivan" in r4.lead
    assert r4.keyboard is not None                          # клавиатуру убираем
    assert store.snapshot("777") is None
    (lead,) = links.leads()
    assert lead.qty == 2 and lead.phone == "+79781234567"
    assert lead.comment == "нужна доставка в Симферополь"


def test_custom_qty_skip_comment_skip_phone(tmp_path):
    links, store, key, code, start, callback, text, contact = _setup(tmp_path)
    start("777", code, USER)
    rc = callback("777", "order:qty:custom", USER)
    assert rc.force_reply is True
    text("777", "5 штук", USER)                             # своё число
    assert store.snapshot("777").qty == 5
    r3 = callback("777", "order:skip_comment", USER)        # пропустить комментарий
    assert "елефон" in r3.text
    r4 = text("777", "Пропустить", USER)                    # пропустить телефон
    assert r4.lead is not None and "Количество: 5 шт." in r4.lead
    assert "Комментарий:" not in r4.lead and "Телефон:" not in r4.lead
    assert store.snapshot("777") is None


def test_text_without_dialog_is_ignored(tmp_path):
    links, store, key, code, start, callback, text, contact = _setup(tmp_path)
    assert text("777", "привет", USER) is None
    assert contact("777", "+79780000000", USER) is None


def test_invalid_custom_qty_reprompts(tmp_path):
    links, store, key, code, start, callback, text, contact = _setup(tmp_path)
    start("777", code, USER)
    callback("777", "order:qty:custom", USER)
    r = text("777", "букашки", USER)
    assert r.force_reply is True
    assert store.snapshot("777").step == "awaiting_qty_custom"
    assert links.leads() == []


def test_skip_comment_before_qty_is_guarded(tmp_path):
    links, store, key, code, start, callback, text, contact = _setup(tmp_path)
    start("777", code, USER)
    r = callback("777", "order:skip_comment", USER)
    assert r.lead is None and "количеств" in r.text.lower()
    assert links.leads() == []


def test_malformed_qty_callback_does_not_crash(tmp_path):
    links, store, key, code, start, callback, text, contact = _setup(tmp_path)
    start("777", code, USER)
    r = callback("777", "order:qty:abc", USER)
    assert r.lead is None
    assert store.snapshot("777").step == "awaiting_qty"


def test_slash_command_not_swallowed_as_comment(tmp_path):
    links, store, key, code, start, callback, text, contact = _setup(tmp_path)
    start("777", code, USER)
    callback("777", "order:qty:2", USER)
    assert text("777", "/status", USER) is None
    assert links.leads() == []
    assert store.snapshot("777") is not None


def test_manual_phone_typed(tmp_path):
    links, store, key, code, start, callback, text, contact = _setup(tmp_path)
    start("777", code, USER)
    callback("777", "order:qty:1", USER)
    callback("777", "order:skip_comment", USER)             # на шаг телефона
    r = text("777", "+7 978 111-22-33", USER)               # телефон текстом
    assert r.lead is not None and "Телефон: +7 978 111-22-33" in r.lead
