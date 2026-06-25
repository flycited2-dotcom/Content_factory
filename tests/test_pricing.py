from decimal import Decimal
from content_factory.models import Offer
from content_factory.pricing.pricing import round_up_90, compute_price, PricingConfig


def _offer(cost, **kw):
    base = dict(supplier_sku="s", source="daichi", brand="B", model="M",
                category_id=2, btu_calc=7, attrs={}, cost=cost, retail_ref=None,
                stock=1, photos=[], series=None, content_hash="")
    base.update(kw)
    return Offer(**base)


def test_round_up_90_examples():
    assert round_up_90(27724.2) == 27790      # из референса
    assert round_up_90(27800) == 27890
    assert round_up_90(27890) == 27890        # уже …90 — не растёт
    assert round_up_90(27891) == 27990


def test_default_markup_5pct():
    cfg = PricingConfig(default_markup_pct=5, min_margin_abs=0, rounding="up_to_90", rules=[])
    r = compute_price(_offer(Decimal("26404")), cfg)
    assert r.ok and r.price == 27790          # 26404*1.05=27724.2 → 27790 (без пола маржи)


def test_min_margin_guard_raises_price():
    cfg = PricingConfig(default_markup_pct=5, min_margin_abs=3000, rounding="up_to_90", rules=[])
    r = compute_price(_offer(Decimal("10000")), cfg)   # +5%=10500 (маржа 500<3000) → 13000 → …90
    assert r.ok and r.min_margin_applied and r.price == 13090


def test_reject_when_cost_missing():
    cfg = PricingConfig(default_markup_pct=5, min_margin_abs=3000, rounding="up_to_90", rules=[])
    r = compute_price(_offer(None), cfg)
    assert r.ok is False and r.reason


def test_rule_override_by_category():
    cfg = PricingConfig(default_markup_pct=5, min_margin_abs=0, rounding="up_to_90",
                        rules=[{"match": {"category_id": 7}, "markup_pct": 30}])
    r = compute_price(_offer(Decimal("10000"), category_id=7), cfg)
    assert r.markup_pct == 30 and r.price == 13090   # 10000*1.30=13000 → 13090
