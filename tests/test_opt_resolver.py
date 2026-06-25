from decimal import Decimal
from content_factory.models import RawProduct
from content_factory.ingest.opt_resolver import resolve_cost


def _raw(**kw):
    base = dict(source="x", nc_code="N", brand="B", title="T", series=None,
                category_id=2, btu_calc=7, price_wholesale=None, price_base=None,
                stock_qty=1, image_urls=[], tech={})
    base.update(kw)
    return RawProduct(**base)


def test_daichi_uses_wholesale():
    r = _raw(source="daichi", price_wholesale=Decimal("100"))
    assert resolve_cost(r, breez_base=None) == Decimal("100")


def test_rusklimat_prefers_price_base():
    r = _raw(source="rusklimat", price_wholesale=Decimal("999"), price_base=Decimal("700"))
    assert resolve_cost(r, breez_base=None) == Decimal("700")


def test_rusklimat_falls_back_to_wholesale():
    r = _raw(source="rusklimat", price_wholesale=Decimal("999"), price_base=None)
    assert resolve_cost(r, breez_base=None) == Decimal("999")


def test_breeze_uses_breez_base_when_present():
    r = _raw(source="breeze", price_wholesale=Decimal("50000"))
    assert resolve_cost(r, breez_base=Decimal("38000")) == Decimal("38000")


def test_breeze_falls_back_to_db():
    r = _raw(source="breeze", price_wholesale=Decimal("50000"))
    assert resolve_cost(r, breez_base=None) == Decimal("50000")


def test_jac_uses_wholesale():
    r = _raw(source="jac", price_wholesale=Decimal("42000"))
    assert resolve_cost(r, breez_base=None) == Decimal("42000")


def test_none_when_nothing():
    r = _raw(source="daichi", price_wholesale=None)
    assert resolve_cost(r, breez_base=None) is None
