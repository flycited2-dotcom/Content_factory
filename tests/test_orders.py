"""Кнопка «📩 Заказать» (волна 1б): ссылки-коды, лиды, разбор /start."""
import json
from content_factory.publish.orders import (
    OrderLinks, order_markup, handle_order_start)
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


def test_handle_order_start_creates_lead(tmp_path):
    ol = OrderLinks(tmp_path / "s.db")
    ps = PublishState(tmp_path / "s.db")
    key = "breeze|funai|kadzoku inverter"
    ps.mark(key, 10, channel="@chan",
            caption="FUNAI серии KADZOKU Inverter\n💰 от 22 390 ₽\n═══\nещё текст")
    code = ol.code_for(key)
    user = {"id": 555, "username": "ivan", "first_name": "Иван"}
    reply, lead = handle_order_start(f"/start ord_{code}", user, ol, ps)
    assert "KADZOKU" in reply and "22 390" in reply          # клиент видит товар и цену
    assert "@ivan" in lead and "KADZOKU" in lead             # лид владельцу
    leads = ol.leads()
    assert len(leads) == 1 and leads[0].user_id == 555 and leads[0].key == key


def test_handle_order_start_unknown_code(tmp_path):
    ol = OrderLinks(tmp_path / "s.db")
    ps = PublishState(tmp_path / "s.db")
    reply, lead = handle_order_start("/start ord_zzz", {"id": 1}, ol, ps)
    assert lead is None and "не найден" in reply.lower()


def test_handle_order_start_not_order(tmp_path):
    ol = OrderLinks(tmp_path / "s.db")
    ps = PublishState(tmp_path / "s.db")
    assert handle_order_start("/start", {"id": 1}, ol, ps) == (None, None)
