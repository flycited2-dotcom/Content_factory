from datetime import datetime, timezone
import pytest
from content_factory.ingest.aru_account import (
    build_account_snapshot,
    load_account_items,
)
from content_factory.ingest.aru_site import save_snapshot
from content_factory.ingest.excel_price import load_price_slots


def page():
    return {
        "page": 1,
        "pages": 1,
        "url": "https://aru.ooo/vse-tovary/",
        "authenticated": True,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "items": [
            {
                "id": "42",
                "article": "001",
                "name": "Перчатки",
                "url": "https://aru.ooo/perchatki/test/",
                "stock_text": "В наличии",
                "price_text": "43,48 ₽",
                "old_price_text": "62,70 ₽",
            }
        ],
    }


def test_prices_and_loader_apply_markup_once(tmp_path):
    data = build_account_snapshot([page()])
    assert data["items"][0]["price"] == "43.48"
    assert data["items"][0]["content_price"] == "47.83"
    assert data["items"][0]["content_price_rub"] == 48
    save_snapshot(tmp_path / "aru-catalog.json", data)
    assert load_account_items(tmp_path)[0].price == 48
    assert load_price_slots(tmp_path)[0][0] == "aru"
    assert load_price_slots(tmp_path)[0][1][0].price == 48


def test_last_successful_prices_survive_delayed_weekly_refresh(tmp_path):
    from datetime import timedelta
    from content_factory.ingest.aru_site import catalog_status
    data = build_account_snapshot([page()])
    data['generated_at'] = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    save_snapshot(tmp_path / 'aru-catalog.json', data)
    assert load_account_items(tmp_path)[0].price == 48
    status = catalog_status(tmp_path)
    assert 'по понедельникам' in status
    assert 'действует последний прайс' in status
    assert 'поиск отключён' not in status


@pytest.mark.parametrize(
    "change", [{"stock_text": "Нет в наличии"}, {"stock_text": ""}, {"price_text": ""}]
)
def test_unavailable_or_unpriced_items_are_excluded(change):
    data = page()
    data["items"][0].update(change)
    result = build_account_snapshot([data])
    assert result["items"] == []
    assert result["excluded_products"] == 1


def test_incomplete_capture_cannot_replace_working_prices(tmp_path):
    data = page()
    data["pages"] = 2
    with pytest.raises(ValueError, match="incomplete"):
        build_account_snapshot([data])
    preview = build_account_snapshot([data], allow_partial=True)
    assert preview["complete"] is False
    save_snapshot(tmp_path / "aru-catalog.json", preview)
    assert load_account_items(tmp_path) == []


def test_logout_duplicate_and_stale_are_rejected():
    data = page()
    data["authenticated"] = False
    with pytest.raises(ValueError, match="unauthenticated"):
        build_account_snapshot([data])
    data = page()
    data["items"].append(dict(data["items"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        build_account_snapshot([data])
    data = page()
    data["captured_at"] = "2000-01-01T00:00:00+00:00"
    with pytest.raises(ValueError, match="stale"):
        build_account_snapshot([data])


def test_moved_product_uses_latest_stock_and_audit():
    from copy import deepcopy
    from datetime import timedelta

    first = page()
    first["pages"] = 2
    second = deepcopy(first)
    second.update(
        page=2,
        url="https://aru.ooo/vse-tovary/?page=2",
        captured_at=(datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
    )
    second["items"][0]["stock_text"] = "Нет в наличии"
    with pytest.raises(ValueError, match="duplicate"):
        build_account_snapshot([first, second])
    data = build_account_snapshot([first, second], reconcile_moved=True)
    assert data["items"] == []
    assert data["scanned_products"] == data["excluded_products"] == 1
    assert data["reconciled_moved_products"] == [{"id": "42", "pages": [1, 2]}]
    # The timestamp, not input order, determines the current observation.
    reverse = build_account_snapshot([second, first], reconcile_moved=True)
    assert reverse["items"] == []
