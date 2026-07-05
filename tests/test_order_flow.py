"""Опросник заказа клиента (задача 3): старт → кол-во (кнопки/своё число) →
комментарий → лид в отдельный чат. Чистая логика без Telegram."""
from content_factory.publish.orders import OrderLinks
from content_factory.publish.telegram import PublishState
from content_factory.bot.order_dialog import OrderDialogStore
from content_factory.bot.order_flow import make_order_flow

USER = {"id": 555, "username": "ivan", "first_name": "Иван"}
CAPTION = ("Микроволновая печь Hyundai HYM-D3008\n"
           "<blockquote>💎 <b>10 672 ₽</b></blockquote>\n═══\nещё текст")


def _setup(tmp_path):
    db = tmp_path / "s.db"
    links = OrderLinks(db)
    ps = PublishState(db)
    store = OrderDialogStore(db)
    key = "excel|hyundai|hym-d3008"
    ps.mark(key, 1, channel="@chan", caption=CAPTION)
    code = links.code_for(key)
    start, callback, text = make_order_flow(store, links, ps)
    return links, store, key, code, start, callback, text


def test_start_shows_item_and_qty_buttons(tmp_path):
    links, store, key, code, start, callback, text = _setup(tmp_path)
    r = start("777", code, USER)
    assert "Hyundai HYM-D3008" in r.text and "10 672" in r.text
    assert r.lead is None
    buttons = [b["callback_data"] for row in r.markup["inline_keyboard"] for b in row]
    assert "order:qty:1" in buttons and "order:qty:custom" in buttons
    assert store.snapshot("777").step == "awaiting_qty"


def test_start_unknown_code_no_dialog(tmp_path):
    links, store, key, code, start, callback, text = _setup(tmp_path)
    r = start("777", "zzz", USER)
    assert "не найден" in r.text.lower() and r.markup is None
    assert store.snapshot("777") is None


def test_happy_path_qty_button_then_comment(tmp_path):
    links, store, key, code, start, callback, text = _setup(tmp_path)
    start("777", code, USER)
    r2 = callback("777", "order:qty:2", USER)
    assert "омментар" in r2.text                        # приглашение к комментарию
    assert store.snapshot("777").qty == 2 and store.snapshot("777").step == "awaiting_comment"
    r3 = text("777", "нужна доставка в Симферополь", USER)
    assert r3.lead is not None
    assert "Количество: 2 шт." in r3.lead
    assert "Комментарий: нужна доставка в Симферополь" in r3.lead
    assert "@ivan" in r3.lead and "Hyundai HYM-D3008" in r3.lead
    assert "Заявка принята" in r3.text and "2 шт." in r3.text
    assert store.snapshot("777") is None                # диалог закрыт
    (lead,) = links.leads()
    assert lead.qty == 2 and lead.comment == "нужна доставка в Симферополь"


def test_custom_qty_then_skip_comment(tmp_path):
    links, store, key, code, start, callback, text = _setup(tmp_path)
    start("777", code, USER)
    rc = callback("777", "order:qty:custom", USER)
    assert rc.force_reply is True
    r2 = text("777", "5 штук", USER)                    # своё число из текста
    assert store.snapshot("777").qty == 5
    r3 = callback("777", "order:skip_comment", USER)
    assert r3.lead is not None and "Количество: 5 шт." in r3.lead
    assert "Комментарий:" not in r3.lead                # пропущен → строки нет
    assert store.snapshot("777") is None


def test_text_without_dialog_is_ignored(tmp_path):
    links, store, key, code, start, callback, text = _setup(tmp_path)
    assert text("777", "привет", USER) is None


def test_invalid_custom_qty_reprompts(tmp_path):
    links, store, key, code, start, callback, text = _setup(tmp_path)
    start("777", code, USER)
    callback("777", "order:qty:custom", USER)
    r = text("777", "букашки", USER)
    assert r.force_reply is True                         # снова просим число
    assert store.snapshot("777").step == "awaiting_qty_custom"
    assert links.leads() == []                           # заявки нет


def test_skip_comment_before_qty_is_guarded(tmp_path):
    links, store, key, code, start, callback, text = _setup(tmp_path)
    start("777", code, USER)
    r = callback("777", "order:skip_comment", USER)
    assert r.lead is None and "количеств" in r.text.lower()
    assert links.leads() == []


def test_malformed_qty_callback_does_not_crash(tmp_path):
    # подделанный callback_data не должен ронять бота (int('abc') → ValueError)
    links, store, key, code, start, callback, text = _setup(tmp_path)
    start("777", code, USER)
    r = callback("777", "order:qty:abc", USER)
    assert r.lead is None                                # ничего не оформили
    assert store.snapshot("777").step == "awaiting_qty"  # остались на выборе кол-ва


def test_slash_command_not_swallowed_as_comment(tmp_path):
    # владелец, кликнувший свою «Заказать», должен уметь выйти командой
    links, store, key, code, start, callback, text = _setup(tmp_path)
    start("777", code, USER)
    callback("777", "order:qty:2", USER)                 # шаг комментария
    assert text("777", "/status", USER) is None          # команда не перехвачена
    assert links.leads() == []                           # левого лида нет
    assert store.snapshot("777") is not None             # диалог ещё жив
