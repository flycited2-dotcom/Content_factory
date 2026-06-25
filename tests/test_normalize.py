from decimal import Decimal
from content_factory.models import RawProduct
from content_factory.ingest.normalize import to_offer, is_conditioner, content_hash, CatalogFilter

FLT = CatalogFilter(report_category_ids=[2, 6, 7],
                    exclude_title_patterns=["%мульти%", "%виброопор%"])


def _raw(**kw):
    base = dict(source="daichi", nc_code="N1", brand="Daichi", title="Сплит-система ABC 07",
                series="ABC", category_id=2, btu_calc=7, price_wholesale=Decimal("100"),
                price_base=None, stock_qty=3, image_urls=["u"], tech={"k": "v"})
    base.update(kw)
    return RawProduct(**base)


def test_is_conditioner_ok():
    assert is_conditioner(_raw(), FLT) is True


def test_filter_excludes_category_116():
    assert is_conditioner(_raw(category_id=116), FLT) is False


def test_filter_excludes_zero_btu():
    assert is_conditioner(_raw(btu_calc=0), FLT) is False


def test_filter_excludes_multisplit_title():
    assert is_conditioner(_raw(title="Мульти сплит 3x"), FLT) is False


def test_to_offer_maps_fields():
    o = to_offer(_raw(), cost=Decimal("100"))
    assert o.supplier_sku == "daichi:N1"
    assert o.cost == Decimal("100")
    assert o.stock == 3 and o.photos == ["u"]
    assert o.content_hash  # непустой


def test_content_hash_stable_and_sensitive():
    o1 = to_offer(_raw(), cost=Decimal("100"))
    o2 = to_offer(_raw(), cost=Decimal("100"))
    o3 = to_offer(_raw(title="Другое"), cost=Decimal("100"))
    assert o1.content_hash == o2.content_hash
    assert o1.content_hash != o3.content_hash
