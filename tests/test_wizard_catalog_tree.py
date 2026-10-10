"""Regression: all in-stock supplier groups and products remain reachable."""
from datetime import datetime, timezone
import json

from content_factory.bot.catalog_tree import build_tree
from content_factory.bot.wizard import WizardStore
from content_factory.bot.wizard_flow import make_wizard_flow
from content_factory.ingest.aru_account import load_account_items
from content_factory.ingest.excel_price import top_sections
from content_factory.orchestrator.excel_pipeline import ExcelStore


def _snapshot(prices):
    prices.mkdir()
    items = []
    for index in range(47):
        items.append({
            "id": str(index + 1), "article": str(index + 1),
            "name": f"Лобзик производительный TOOL J{index:03d}",
            "brand": "TOOL", "price": "100.01", "available": True,
            "price_basis": "account_price",
            "category_path": ["Электроинструмент", "Лобзики"],
            "category_ids": ["elektroinstrument", "elektroinstrument/lobziki"],
        })
    items.extend([
        {**items[0], "id": "100", "name": "Перфоратор TOOL P100",
         "category_path": ["Электроинструмент", "Перфораторы"],
         "category_ids": ["elektroinstrument", "elektroinstrument/perforatory"]},
        {**items[0], "id": "101", "name": "Лобзик нет в наличии",
         "available": False},
        {**items[0], "id": "102", "name": "Лобзик под заказ", "available": None},
    ])
    (prices / "aru-catalog.json").write_text(json.dumps({
        "source": "aru", "complete": True, "authenticated": True,
        "price_basis": "account_price", "generated_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
    }, ensure_ascii=False), encoding="utf-8")


def _flow(tmp_path):
    prices = tmp_path / "prices"
    _snapshot(prices)
    store = WizardStore(tmp_path / "state.db")
    flow = make_wizard_flow(tmp_path / "state.db", prices, store,
                            lambda *args: 0, lambda *args: "", lambda: "СТАТУС")
    return flow, store


def _button(reply, text):
    return next(button["callback_data"] for row in reply.markup["inline_keyboard"]
                for button in row if button["text"] == text)


def _action(reply, prefix):
    return next(button["callback_data"] for row in reply.markup["inline_keyboard"]
                for button in row if button["callback_data"].startswith("wizard:" + prefix))


def _lobziki(start, callback):
    sources = start("1")
    root = callback("1", _action(sources, "source:"))
    assert "Лобзики" not in str(root.markup)
    group = callback("1", _button(root, "Электроинструмент"))
    assert "Перфораторы" in str(group.markup)
    return callback("1", _button(group, "Лобзики"))


def test_hierarchy_filters_exact_leaf_and_all_products_have_pages(tmp_path):
    (start, _, _, callback), store = _flow(tmp_path)
    page = _lobziki(start, callback)
    assert "доступно 47" in page.text and "1/3" in page.text
    assert len(store.snapshot("1").candidates) == 47
    assert "Перфоратор" not in page.text and "под заказ" not in page.text
    page = callback("1", _button(page, "Ещё ▸"))
    assert "2/3" in page.text and "J020" in page.text
    page = callback("1", _button(page, "Ещё ▸"))
    assert "3/3" in page.text and "J046" in page.text
    assert "111 ₽" in page.text  # ceil(account100.01 *1.10), once only


def test_selection_survives_pages_restart_and_page_bulk_is_scoped(tmp_path):
    (start, _, _, callback), store = _flow(tmp_path)
    page = _lobziki(start, callback)
    page = callback("1", _button(page, "▫️ 1"))
    page = callback("1", _button(page, "Ещё ▸"))
    page = callback("1", _button(page, "✅ Выбрать страницу"))
    assert len(store.snapshot("1").selected_keys) == 21
    # SQLite migration and restart retain both the current page and basket.
    restarted = WizardStore(tmp_path / "state.db")
    assert restarted.snapshot("1").page == 1
    assert len(restarted.snapshot("1").selected_keys) == 21
    _, _, _, callback = make_wizard_flow(tmp_path / "state.db", tmp_path / "prices", restarted,
                                         lambda *args: 0, lambda *args: "", lambda: "СТАТУС")
    page = callback("1", _button(page, "☐ Снять страницу"))
    assert len(restarted.snapshot("1").selected_keys) == 1
    page = callback("1", _button(page, "▫️ 21"))
    callback("1", _action(page, "selection_continue:"))
    chosen = restarted.snapshot("1")
    assert chosen.step == "awaiting_time"
    assert [candidate[3].rsplit(" ", 1)[-1] for candidate in chosen.candidates] == ["J000", "J020"]


def test_stale_checkbox_cannot_select_another_categories_product(tmp_path):
    (start, _, _, callback), store = _flow(tmp_path)
    old = _lobziki(start, callback)
    root = callback("1", "wizard:categories")
    group = callback("1", _button(root, "Электроинструмент"))
    callback("1", _button(group, "Перфораторы"))
    result = callback("1", _button(old, "▫️ 1"))
    assert "устарел" in result.text
    assert store.snapshot("1").selected_keys == []


def test_long_supplier_path_and_same_named_subgroups_are_not_lost(tmp_path):
    prices = tmp_path / "prices"
    _snapshot(prices)
    data = json.loads((prices / "aru-catalog.json").read_text(encoding="utf-8"))
    data["items"][0]["category_path"] = ["Очень длинное название корневого раздела поставщика",
                                        "Очень длинное название подраздела лобзиков поставщика"]
    data["items"][0]["category_ids"] = ["long", "long/lobziki"]
    data["items"][1]["category_path"] = ["Ручной инструмент", "Лобзики"]
    data["items"][1]["category_ids"] = ["ruchnoy", "ruchnoy/lobziki"]
    (prices / "aru-catalog.json").write_text(json.dumps(data), encoding="utf-8")
    items = load_account_items(prices)
    assert len(items) == 48
    assert items[0].section in top_sections(prices)
    tree = build_tree(items)
    assert tree["ruchnoy/lobziki"].parent == "ruchnoy"
    assert tree["elektroinstrument/lobziki"].parent == "elektroinstrument"
    assert tree["ruchnoy/lobziki"].item_indices != tree["elektroinstrument/lobziki"].item_indices


def test_snapshot_taxonomy_labels_legacy_slug_row(tmp_path):
    prices = tmp_path / "prices"
    _snapshot(prices)
    data = json.loads((prices / "aru-catalog.json").read_text(encoding="utf-8"))
    data["items"][0]["category_path"] = ["elektroinstrument", "lobziki"]
    data["categories"] = [{"id": "elektroinstrument/lobziki",
                            "path_names": ["Электроинструмент", "Лобзики"],
                            "path_ids": ["elektroinstrument", "elektroinstrument/lobziki"]}]
    (prices / "aru-catalog.json").write_text(json.dumps(data), encoding="utf-8")
    assert load_account_items(prices)[0].category_path == ("Электроинструмент", "Лобзики")


def test_bulk_confirmation_keeps_full_queue_and_bounded_telegram_receipt(tmp_path):
    (start, _, _, callback), store = _flow(tmp_path)
    snapshot = tmp_path / "prices/aru-catalog.json"
    data = json.loads(snapshot.read_text(encoding="utf-8"))
    for row in data["items"]:
        row["name"] = "Лобзик " + "многофункциональный " * 6 + row["name"]
    snapshot.write_text(json.dumps(data), encoding="utf-8")
    page = _lobziki(start, callback)
    callback("1", _button(page, "✅ Все 47"))
    callback("1", "wizard:time_now")
    callback("1", "wizard:skip_photo")
    callback("1", "wizard:skip_utp")
    result = callback("1", "wizard:confirm")
    assert len(ExcelStore(tmp_path / "state.db").by_status("new")) == 47
    assert "ещё 27" in result.text and len(result.text) < 4096
    assert store.snapshot("1") is None
