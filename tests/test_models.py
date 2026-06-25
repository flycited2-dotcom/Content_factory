from decimal import Decimal
from content_factory.models import RawProduct, Offer, PriceResult, Content, AdRecord, City


def test_offer_roundtrip_and_defaults():
    o = Offer(supplier_sku="rusklimat:NC123", source="rusklimat", brand="Ballu",
              model="Olympio Edge BSO-07HN8", category_id=2, btu_calc=7,
              attrs={"Холод, кВт": "2.05"}, cost=Decimal("26404"), retail_ref=None,
              stock=4, photos=["https://x/y.jpg"], series="Olympio Edge", content_hash="")
    assert o.cost == Decimal("26404")
    assert o.stock == 4
    assert o.photos == ["https://x/y.jpg"]


def test_raw_product_optional_prices():
    r = RawProduct(source="breeze", nc_code="NC9", brand="Бриз", title="T", series=None,
                   category_id=2, btu_calc=9, price_wholesale=Decimal("30000"),
                   price_base=None, stock_qty=2, image_urls=[], tech={})
    assert r.price_base is None
    assert r.stock_qty == 2


def test_price_result_reject():
    p = PriceResult(ok=False, price=None, markup_pct=5, min_margin_applied=False, reason="cost<=0")
    assert p.ok is False and p.reason == "cost<=0"


def test_city_fields():
    c = City(id="simferopol", name="Симферополь", avito_location="Симферополь")
    assert c.avito_location == "Симферополь"
