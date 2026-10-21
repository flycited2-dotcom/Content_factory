"""Повтор товара из прайса — не дубль (владелец, 2026-10-20): через 3 дня после
публикации/отклонения или сразу при другой цене. Ждущие решения и идущие по
конвейеру позиции по-прежнему заняты."""
import time

from content_factory.ingest.excel_price import PriceItem, item_is_taken, search_items
from content_factory.orchestrator import excel_pipeline
from content_factory.orchestrator.confirm_store import ConfirmStore
from content_factory.orchestrator.excel_pipeline import ExcelStore
from content_factory.publish.telegram import PublishState

KEY = "excel|indesit|ds 3180 g"
ROW = (KEY, "Indesit", "DS 3180 G", "Холодильник Indesit DS 3180 G", 28600)
DAY = 86400


def _published(tmp_path, ago_days, price=28600):
    db = tmp_path / "s.db"
    store = ExcelStore(db)
    assert store.select_items([ROW[:4] + (price,)])
    store.update(KEY, status="preview")
    ConfirmStore(db).add(KEY, "chan", "card.png", "caption")
    ConfirmStore(db).mark(KEY, "published")
    PublishState(db).mark(KEY, 1, channel="chan")
    ts = time.time() - ago_days * DAY
    import sqlite3
    with sqlite3.connect(db) as c:
        c.execute("UPDATE published SET ts=?", (ts,))
        c.execute("UPDATE awaiting SET ts=?", (ts,))
    return db, store


def _taken(db, store):
    return store.selection_taken(PublishState(db).published_keys()
                                 | ConfirmStore(db).blocked_keys())


def _item(price=28600, brand=""):
    return PriceItem("Холодильники", "1", brand, "Холодильник Indesit DS 3180 G (185*60*64)", price)


def test_recent_same_price_is_still_a_duplicate(tmp_path):
    db, store = _published(tmp_path, ago_days=1)
    assert item_is_taken(_item(), _taken(db, store))
    assert store.select_items([ROW]) == []


def test_recent_but_new_price_repeats_now(tmp_path):
    db, store = _published(tmp_path, ago_days=1)
    taken = _taken(db, store)
    assert not item_is_taken(_item(price=27000), taken)
    assert not item_is_taken(_item(price=27000, brand="Indesit"), taken)
    assert store.select_items([ROW[:4] + (27000,)])
    assert store.get(KEY).status == "new" and store.get(KEY).price == 27000


def test_after_three_days_repeats_even_at_same_price(tmp_path):
    db, store = _published(tmp_path, ago_days=excel_pipeline.REPEAT_AFTER / DAY + 0.1)
    assert not item_is_taken(_item(), _taken(db, store))
    assert store.select_items([ROW])
    assert store.get(KEY).status == "new"


def test_pending_preview_is_never_repeated(tmp_path):
    db, store = _published(tmp_path, ago_days=10)
    ConfirmStore(db).add(KEY, "chan", "card2.png", "caption")            # снова ждёт кнопки
    assert item_is_taken(_item(price=1), _taken(db, store))
    assert store.select_items([ROW[:4] + (1,)]) == []


def test_item_in_pipeline_is_never_repeated(tmp_path):
    db, store = _published(tmp_path, ago_days=10)
    store.update(KEY, status="research")
    assert item_is_taken(_item(price=1), _taken(db, store))


def test_section_search_offers_old_publication_again(tmp_path):
    db, store = _published(tmp_path, ago_days=5)
    found = search_items([_item()], "Холодильники", _taken(db, store))
    assert len(found) == 1


def test_publish_idempotency_window_allows_later_repeat(tmp_path):
    state = PublishState(tmp_path / "p.db")
    state.mark(KEY, 1)
    assert state.is_published(KEY) and state.is_published(KEY, within=600)
    import sqlite3
    with sqlite3.connect(tmp_path / "p.db") as c:
        c.execute("UPDATE published SET ts=?", (time.time() - 3600,))
    assert not state.is_published(KEY, within=600)
