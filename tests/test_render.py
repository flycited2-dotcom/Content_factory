from decimal import Decimal
from content_factory.models import Offer
from content_factory.config import ContentConfig
from content_factory.catalog.series import group_by_series
from content_factory.content.render import render_caption


def _offer(sku="breeze:NC1", brand="Ballu", model="Olympio", btu=9, cat=2, series="Olympio"):
    return Offer(supplier_sku=sku, source="breeze", brand=brand, model=model,
                 category_id=cat, btu_calc=btu, attrs={"Холод, кВт": "2.6"},
                 cost=Decimal("20000"), stock=1, photos=[], series=series)


CFG = ContentConfig(caption_max=1024, stop_words=["звоните"], descriptions={})


def test_caption_has_brand_type_and_price():
    cap = render_caption(_offer(), 25990, CFG)
    assert "Ballu" in cap
    assert "Olympio" in cap
    assert "25 990" in cap and "₽" in cap


def test_caption_within_limit():
    cap = render_caption(_offer(), 25990, CFG)
    assert 0 < len(cap) <= CFG.caption_max


def test_caption_shows_power():
    cap = render_caption(_offer(btu=9, cat=2), 25990, CFG)
    assert "BTU" in cap                       # мощность/площадь показаны


def test_caption_deterministic():
    o = _offer(sku="breeze:NC9")
    assert render_caption(o, 25990, CFG) == render_caption(o, 25990, CFG)


def test_caption_strips_stopwords_and_applies_override():
    cfg = ContentConfig(caption_max=1024, stop_words=["звоните"],
                        descriptions={"breeze|ballu|olympio": "Отличная модель. звоните"})
    group = group_by_series([_offer()])[0]
    cap = render_caption(group, 25990, cfg)
    assert "звоните" not in cap.lower()       # стоп-слово вычищено
    assert "Отличная модель" in cap            # ручной текст применён
    assert "25 990" in cap                     # живая цена дописана к override


def test_caption_no_price_ok():
    cap = render_caption(_offer(), None, CFG)
    assert "Ballu" in cap and len(cap) <= 1024


def test_caption_long_override_truncated_keeps_price():
    cfg = ContentConfig(caption_max=1024, stop_words=[],
                        descriptions={"breeze|ballu|olympio": "А" * 5000})
    group = group_by_series([_offer()])[0]
    cap = render_caption(group, 25990, cfg)
    assert len(cap) <= 1024
    assert "25 990" in cap                     # цена сохранена даже при длинном override
