"""Кнопка «💰 Изменить цену» на превью (запрос владельца 2026-07-07): владелец
вводит новую цену → подпись обновляется, превью пересылается и снова висит на
подтверждении (✅/❌/🔄/💰). Только для единичных товаров из прайса (excel|*),
серии кондиционеров с линейкой цен не трогаем."""
import httpx

from content_factory.bot import run as botrun
from content_factory.bot.commands import handle_command
from content_factory.orchestrator.confirm_store import ConfirmStore
from content_factory.orchestrator.excel_run import (
    build_preview_caption, replace_price_in_caption)
from content_factory.orchestrator.queue import TaskQueue
from content_factory.publish.orders import OrderLinks


def test_replace_price_keeps_name_and_utp():
    cap = build_preview_caption("Холодильник LG GA-B419SLGL", 30049, "✓ No Frost")
    new = replace_price_in_caption(cap, 25990)
    assert "25 990 ₽" in new
    assert "30 049" not in new
    assert "Холодильник LG GA-B419SLGL" in new
    assert "No Frost" in new


def test_handle_price_command_routes_to_fn(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    got = {}
    reply = handle_command("/price excel|lg|x 25990", q,
                           price_fn=lambda key, price: got.update(k=key, p=price)
                           or "💰 ок")
    assert reply == "💰 ок"
    assert got == {"k": "excel|lg|x", "p": 25990}


def test_handle_price_command_rejects_bad_price(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    assert "❌" in handle_command("/price excel|lg|x дорого", q,
                                 price_fn=lambda k, p: "не должен вызваться")
    assert "❌" in handle_command("/price excel|lg|x", q,
                                 price_fn=lambda k, p: "не должен вызваться")


def test_make_price_fn_updates_caption_and_resends_preview(tmp_path):
    cs = ConfirmStore(tmp_path / "state.db")
    links = OrderLinks(tmp_path / "state.db")
    card = tmp_path / "card.jpg"
    card.write_bytes(b"IMG")
    cap = build_preview_caption("Холодильник LG X", 30049, "✓ УТП")
    cs.add("excel|lg|x", "@chan", str(card), cap)

    sent = {}

    def handler(req):
        sent["path"] = req.url.path
        sent["body"] = req.read().decode(errors="ignore")
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})
    http = httpx.Client(transport=httpx.MockTransport(handler),
                        base_url="https://api.telegram.org")

    price_fn = botrun.make_price_fn(tmp_path / "state.db", "TOK", "@review",
                                    "HTML", links, http=http)
    reply = price_fn("excel|lg|x", 25990)

    assert "25 990" in reply
    a = cs.get("excel|lg|x")
    assert "25 990 ₽" in a.caption and a.status == "pending"   # сохранено на подтверждении
    assert sent["path"].endswith("/sendPhoto")                 # свежее превью отправлено
    assert "price:" in sent["body"]                            # кнопка цены на месте


def test_make_price_fn_rejects_non_excel_and_missing(tmp_path):
    cs = ConfirmStore(tmp_path / "state.db")
    links = OrderLinks(tmp_path / "state.db")
    cs.add("breeze|ballu|olympio", "@chan", "/c/x.jpg", "cap")
    http = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"ok": True})),
        base_url="https://api.telegram.org")
    price_fn = botrun.make_price_fn(tmp_path / "state.db", "TOK", "@review",
                                    "HTML", links, http=http)
    assert "❌" in price_fn("breeze|ballu|olympio", 100)   # серии — не для этой кнопки
    assert "❌" in price_fn("excel|нет|такого", 100)       # нет на подтверждении
