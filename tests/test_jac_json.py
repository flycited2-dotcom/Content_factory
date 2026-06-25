from pathlib import Path
from decimal import Decimal
from content_factory.ingest.jac_json import load_jac_offers

FIX = Path(__file__).parent / "fixtures" / "jac_stock_sample.json"


def test_loads_only_conditioners_in_stock():
    offers = load_jac_offers(FIX)
    skus = {o.supplier_sku for o in offers}
    assert "jac:MDV-AB-07" in skus            # бытовой сплит — взят
    assert "jac:MDV-MULTI-2" not in skus       # мультисплит — отсеян
    assert "jac:ACC-1" not in skus             # аксессуар — отсеян


def test_jac_cost_is_price():
    o = next(o for o in load_jac_offers(FIX) if o.supplier_sku == "jac:MDV-AB-07")
    assert o.cost == Decimal("42000")
    assert o.stock == 5 and o.source == "jac"


def test_missing_file_returns_empty():
    assert load_jac_offers(Path("nope.json")) == []
