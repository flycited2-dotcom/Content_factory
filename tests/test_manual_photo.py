"""Ручное фото товара ответом на превью (владелец, 2026-07-14): research-фото
для прайс-позиций — фантазия модели (бойлеры Ballu вышли «генериками»), владелец
отвечает на превью реальным фото → оно в research_cache (source='manual') и
позиция уходит на перегенерацию карточки штатной regen-логикой."""
import sqlite3
from pathlib import Path

from content_factory.bot.manual_photo import make_manual_photo_fn, preview_code_from_reply
from content_factory.bot.run import make_regen_fn
from content_factory.orchestrator.confirm_store import ConfirmStore
from content_factory.orchestrator.excel_pipeline import ExcelStore
from content_factory.publish.orders import OrderLinks


def _msg_with_reply(code: str) -> dict:
    return {"reply_to_message": {"reply_markup": {"inline_keyboard": [
        [{"text": "✅", "callback_data": f"approve:{code}"},
         {"text": "❌", "callback_data": f"reject:{code}"}],
    ]}}}


def test_preview_code_from_reply():
    assert preview_code_from_reply(_msg_with_reply("abc123")) == "abc123"
    assert preview_code_from_reply({}) is None
    assert preview_code_from_reply({"reply_to_message": {}}) is None
    # чужая клавиатура без наших действий — не превью
    assert preview_code_from_reply({"reply_to_message": {"reply_markup": {
        "inline_keyboard": [[{"text": "x", "callback_data": "other:1"}]]}}}) is None


def _setup(tmp_path):
    db = tmp_path / "state.db"
    store = ExcelStore(db)
    store.add_items([("excel|ballu|bwh/s 100 shell", "Ballu", "BWH/S 100 Shell",
                      "Водонагреватель Ballu BWH/S 100 Shell", 15000)])
    store.update("excel|ballu|bwh/s 100 shell", status="preview")
    store.cache_put("ballu|bwh/s 100 shell", "✓ Бак 100 л", "research_1.png")

    card = tmp_path / "card.jpg"
    card.write_bytes(b"old-card")
    cs = ConfirmStore(db)
    cs.add("excel|ballu|bwh/s 100 shell", "-100", str(card), "Водонагреватель…")

    links = OrderLinks(db)
    code = links.code_for("excel|ballu|bwh/s 100 shell")

    fn = make_manual_photo_fn(db, links, cs, make_regen_fn(tmp_path / "cards.db", db),
                              tmp_path / "manual_photos")
    return db, store, cs, card, code, fn


def test_manual_photo_full_flow(tmp_path):
    db, store, cs, card, code, fn = _setup(tmp_path)
    reply = fn(_msg_with_reply(code), b"real-photo-bytes")

    assert "перегенерирована" in reply
    # фото сохранено и попало в кэш как manual (utp сохранён прежний)
    saved = list((tmp_path / "manual_photos").glob("*.jpg"))
    assert len(saved) == 1 and saved[0].read_bytes() == b"real-photo-bytes"
    utp, photo = store.cache_get("ballu|bwh/s 100 shell")
    assert utp == "✓ Бак 100 л" and photo == str(saved[0])
    with sqlite3.connect(db) as c:
        src = c.execute("SELECT source FROM research_cache WHERE model_key=?",
                        ("ballu|bwh/s 100 shell",)).fetchone()[0]
    assert src == "manual"
    # research больше НЕ перезапишет ручное фото
    store.cache_put("ballu|bwh/s 100 shell", "другое", "research_2.png")
    assert store.cache_get("ballu|bwh/s 100 shell")[1] == str(saved[0])
    # позиция ушла на пересборку, превью помечено, старая карточка снесена
    item = store.get("excel|ballu|bwh/s 100 shell")
    assert item.status == "new" and item.tries == 0
    assert cs.get("excel|ballu|bwh/s 100 shell").status == "regen"
    assert not card.exists()


def test_photo_without_reply_is_not_ours(tmp_path):
    *_, fn = _setup(tmp_path)
    assert fn({"photo": [{"file_id": "x"}]}, b"bytes") is None   # пусть решает визард


def test_unknown_code(tmp_path):
    *_, fn = _setup(tmp_path)
    assert "не нашёл" in fn(_msg_with_reply("nope000000"), b"bytes")
