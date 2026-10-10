"""Full category-tree snapshot shows Russian labels, never URL slugs."""
from datetime import datetime, timezone
import json

from content_factory.bot.wizard import WizardStore
from content_factory.bot.wizard_flow import make_wizard_flow
from content_factory.bot.catalog_tree import build_tree
from content_factory.ingest.excel_price import PriceItem


def test_russian_root_and_subgroups_resolve_taxonomy_with_url_prefix_ids(tmp_path):
    prices = tmp_path / "prices"
    prices.mkdir()
    ids = ["/elektroinstrument/", "/elektroinstrument/perforatory/"]
    # Even a legacy slug path gets the Russian names from authoritative taxonomy.
    snapshot = {
        "schema_version": 1, "source": "aru", "complete": True,
        "authenticated": True, "price_basis": "account_price",
        "completeness_basis": "category_tree", "generated_at": datetime.now(timezone.utc).isoformat(),
        "categories": [{"id": ids[0], "name": "Электроинструмент", "parent_id": None,
                        "path_ids": ids[:1], "path_names": ["Электроинструмент"]},
                       {"id": ids[1], "name": "Перфораторы", "parent_id": ids[0],
                        "path_ids": ids, "path_names": ["Электроинструмент", "Перфораторы"]}],
        "items": [{"id": "900", "article": "P900", "name": "Перфоратор TOOL P900",
                   "url": "https://aru.ooo/elektroinstrument/perforatory/p900/", "brand": "TOOL",
                   "available": True, "stock_text": "В наличии", "price_basis": "account_price",
                   "price": "100.01", "category_ids": ids,
                   "category_path": ["elektroinstrument", "perforatory"]}],
    }
    (prices / "aru-catalog.json").write_text(json.dumps(snapshot), encoding="utf-8")
    store = WizardStore(tmp_path / "state.db")
    start, _, _, callback = make_wizard_flow(tmp_path / "state.db", prices, store,
                                             lambda *args: 0, lambda *args: "", lambda: "СТАТУС")
    sources = start("owner")
    root = callback("owner", next(button["callback_data"] for row in sources.markup["inline_keyboard"]
                                  for button in row if button["callback_data"].startswith("wizard:source:")))
    buttons = [button for row in root.markup["inline_keyboard"] for button in row]
    assert not any("elektroinstrument" in button["text"] for button in buttons)
    group = callback("owner", next(button["callback_data"] for button in buttons
                                     if button["text"] == "Электроинструмент"))
    buttons = [button for row in group.markup["inline_keyboard"] for button in row]
    assert not any("perforatory" in button["text"] for button in buttons)
    products = callback("owner", next(button["callback_data"] for button in buttons
                                        if button["text"] == "Перфораторы"))
    assert "Электроинструмент / Перфораторы" in products.text
    assert store.snapshot("owner").candidates[0][4] == 111


def test_one_level_russian_supplier_group_has_no_empty_flat_duplicate():
    item = PriceItem(section="Перчатки", article="G1", brand="", name="Перчатки G1", price=100,
                     category_path=("Перчатки",), category_ids=("/perchatki/",))
    tree = build_tree([item], ["Перчатки"])
    assert tree[""].children == ["/perchatki/"]
    assert tree["/perchatki/"].name == "Перчатки"
    assert tree["/perchatki/"].item_indices == [0]
