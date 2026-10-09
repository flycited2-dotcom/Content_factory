from datetime import datetime, timezone

import pytest

from content_factory.ingest.aru_account import (
    build_account_snapshot,
    load_account_items,
    merge_supplement,
)
from content_factory.ingest.aru_site import save_snapshot

NOW = datetime.now(timezone.utc).isoformat()


def base_snapshot():
    page = {
        "page": 1, "pages": 1, "url": "https://aru.ooo/vse-tovary/", "authenticated": True,
        "captured_at": NOW,
        "items": [{"id": "42", "article": "001", "name": "Перчатки",
                   "url": "https://aru.ooo/perchatki/test/", "stock_text": "В наличии",
                   "price_text": "43,48 ₽"}],
    }
    return build_account_snapshot([page])


def row(ident="70001", **extra):
    base = {
        "id": ident, "article": "A1", "name": "Перфоратор DCK", "brand": "DCK",
        "url": f"https://aru.ooo/elektroinstrument/dck/setevoy-instrument/perforator-{ident}/",
        "stock_text": "В наличии", "price_text": "6 365,68 ₽", "old_price_text": "7 000 ₽",
        "specifications": {"Бренд": "DCK"}, "image_urls": ["https://aru.ooo/x.jpg"],
        "description": "d", "captured_at": NOW,
    }
    base.update(extra)
    return base


def merge(rows, base=None, **kw):
    return merge_supplement(base or base_snapshot(), rows, authenticated=True, **kw)


def test_adds_eligible_rows_with_the_same_price_rules_as_the_main_snapshot():
    data = merge([row()])
    added = next(i for i in data["items"] if i["id"] == "70001")
    assert added["price"] == "6365.68"
    assert added["content_price_rub"] == 7003          # 6365.68 * 1.10 = 7002.248 → вверх
    assert added["content_price"] == "7002.25"
    assert added["category_path"] == ["elektroinstrument", "dck", "setevoy-instrument"]
    assert added["available"] is True and added["price_basis"] == "account_price"
    assert [i["id"] for i in data["items"]] == ["42", "70001"]   # старые товары на месте


def test_unavailable_and_unpriced_rows_are_counted_not_added():
    data = merge([row("1", stock_text="Нет в наличии"), row("2", price_text=""), row("3")])
    assert [i["id"] for i in data["items"]] == ["42", "3"]
    assert data["excluded_products"] == base_snapshot()["excluded_products"] + 2
    assert data["scanned_products"] == base_snapshot()["scanned_products"] + 3


def test_ids_already_in_base_are_not_duplicated_or_overridden():
    data = merge([row("42", name="Другое имя", price_text="1 ₽")])
    assert len(data["items"]) == 1 and data["items"][0]["name"] == "Перчатки"


def test_header_keeps_supplier_date_and_records_supplement():
    base = base_snapshot()
    data = merge([row()], base=base)
    assert data["generated_at"] == base["generated_at"]
    assert data["complete"] is True and data["authenticated"] is True
    assert data["supplement"]["items"] == 1
    assert data["supplement"]["sections"] == ["elektroinstrument"]


def test_result_is_loadable_and_prices_apply_markup_once(tmp_path):
    save_snapshot(tmp_path / "aru-catalog.json", merge([row()]))
    by_article = {i.article: i.price for i in load_account_items(tmp_path)}
    assert by_article == {"001": 48, "A1": 7003}


def test_input_snapshot_is_not_mutated():
    base = base_snapshot()
    before = len(base["items"])
    merge([row()], base=base)
    assert len(base["items"]) == before


@pytest.mark.parametrize("bad", [
    {"url": "https://evil.example/x/"},
    {"url": "http://aru.ooo/x/"},
    {"id": "abc"},
    {"name": ""},
])
def test_invalid_rows_are_rejected(bad):
    with pytest.raises(ValueError):
        merge([row(**bad)])


def test_duplicate_ids_in_supplement_are_rejected():
    with pytest.raises(ValueError):
        merge([row("7"), row("7")])


def test_requires_authenticated_capture():
    with pytest.raises(ValueError):
        merge_supplement(base_snapshot(), [row()], authenticated=False)


def test_rejects_non_aru_or_incomplete_base():
    base = base_snapshot()
    base["complete"] = False
    with pytest.raises(ValueError):
        merge([row()], base=base)
