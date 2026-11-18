import time

from content_factory.orchestrator.vk_catalog_plan import load_catalog_items, retire_excluded
from content_factory.orchestrator.vk_content_plan import VkContentPlanStore, VkPlanCandidate
from content_factory.storefront.product_posts import CatalogItem


def _item(item_id):
    return CatalogItem(id=item_id, url="u", price=1, group="ac", category="c", picture="",
                       name=item_id, brand="B", prose="")


def test_excluded_supplier_is_filtered_and_retired(tmp_path, monkeypatch):
    monkeypatch.setenv("VK_CATALOG_EXCLUDE_SUPPLIERS", "rusklimat")
    import content_factory.storefront.product_posts as pp
    monkeypatch.setattr(pp, "load_catalog", lambda directory: [
        _item("rusklimat:NS-1"), _item("breeze:NS-2"), _item("daichi:D-3")])
    items, report = load_catalog_items(tmp_path, None)
    assert [i.id for i in items] == ["breeze:NS-2", "daichi:D-3"]
    assert report["excluded_suppliers"] == ["rusklimat"]

    store = VkContentPlanStore(tmp_path / "plan.db")
    due = int(time.time()) + 3600
    ids = [store.add(VkPlanCandidate(source_key=f"catalog:{sup}:NS-{n}", source_ts=float(due),
                                     caption=f"Товар {sup} {n}\n💎 1 ₽", card_path="c.jpg",
                                     category="heaters", brand="B"), due + n)
           for n, sup in enumerate(("rusklimat", "breeze"))]
    assert retire_excluded(store) == [ids[0]]
    assert store.get(ids[0]).status == "superseded"
    assert store.get(ids[1]).status == "planned"
    assert retire_excluded(store) == []


def test_no_exclusion_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("VK_CATALOG_EXCLUDE_SUPPLIERS", raising=False)
    store = VkContentPlanStore(tmp_path / "plan.db")
    assert retire_excluded(store) == []
