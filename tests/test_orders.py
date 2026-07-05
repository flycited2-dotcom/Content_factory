"""Кнопка «📩 Заказать»: коды-ссылки, лиды (кол-во+комментарий), plain-text сводка."""
import json
from content_factory.publish.orders import OrderLinks, order_markup, item_summary
from content_factory.publish.telegram import PublishState


def test_code_stable_and_short(tmp_path):
    ol = OrderLinks(tmp_path / "s.db")
    code = ol.code_for("breeze|funai|kadzoku inverter")
    assert code == ol.code_for("breeze|funai|kadzoku inverter")   # детерминированный
    assert 4 <= len(code) <= 32 and code.isalnum()
    assert ol.key_for(code) == "breeze|funai|kadzoku inverter"
    assert ol.key_for("nope") is None


def test_order_markup_url_button(tmp_path):
    ol = OrderLinks(tmp_path / "s.db")
    kb = json.loads(order_markup("Sendpr1ce_bot", ol.code_for("k1")))
    btn = kb["inline_keyboard"][0][0]
    assert btn["text"].startswith("📩")
    assert btn["url"].startswith("https://t.me/Sendpr1ce_bot?start=ord_")


def test_add_lead_stores_qty_comment_phone(tmp_path):
    ol = OrderLinks(tmp_path / "s.db")
    ol.add_lead(555, "ivan", "breeze|funai|x", qty=3,
                comment="нужна доставка", phone="+79781234567")
    (lead,) = ol.leads()
    assert lead.user_id == 555 and lead.key == "breeze|funai|x"
    assert lead.qty == 3 and lead.comment == "нужна доставка"
    assert lead.phone == "+79781234567"


def test_add_lead_defaults(tmp_path):
    ol = OrderLinks(tmp_path / "s.db")
    ol.add_lead(1, "", "k")
    (lead,) = ol.leads()
    assert lead.qty == 1 and lead.comment == "" and lead.phone == ""


def test_item_summary_strips_html(tmp_path):
    ps = PublishState(tmp_path / "s.db")
    ps.mark("breeze|funai|kadzoku", 10, channel="@chan",
            caption=("FUNAI серии KADZOKU &lt;RAC-07&gt;\n"
                     "<blockquote>💎 <b>от 22 390 ₽</b></blockquote>\n═══\nещё текст"))
    summary = item_summary(ps, "breeze|funai|kadzoku")
    assert "<b>" not in summary and "<blockquote>" not in summary   # HTML вычищен
    assert "&lt;" not in summary                                    # сущности раскрыты
    assert "FUNAI серии KADZOKU <RAC-07>" in summary
    assert "💎 от 22 390 ₽" in summary


def test_item_summary_unknown_key_returns_key(tmp_path):
    ps = PublishState(tmp_path / "s.db")
    assert item_summary(ps, "нет-такого") == "нет-такого"
