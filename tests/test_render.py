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


def test_caption_uses_real_specs_btu_kw_area():
    # btu_calc=7 (битый), но реальные ТТХ kBTU=8 / 2.2 кВт / 22 м² должны победить
    o = Offer(supplier_sku="breeze:DA25", source="breeze", brand="FUNAI",
              model="DAIJIN Inverter", category_id=2, btu_calc=7,
              attrs={"Холодопроизводительность (kBTU)": "8",
                     "Холодопроизводительность (кВт)": "2.20 (0.30 - 2.85)",
                     "Эффективен для помещений площадью до": "22"},
              cost=Decimal("37290"), stock=38, photos=[], series="DAIJIN Inverter")
    cap = render_caption(o, 49990, CFG)
    assert "8000 BTU" in cap
    assert "2.2 кВт" in cap
    assert "до 22 м²" in cap
    assert "7000 BTU" not in cap              # btu_calc не используется при наличии ТТХ


def test_caption_real_specs_decimal_area():
    o = Offer(supplier_sku="breeze:DA65", source="breeze", brand="FUNAI", model="DAIJIN",
              category_id=2, btu_calc=20,
              attrs={"Холодопроизводительность (kBTU)": "21",
                     "Холодопроизводительность (кВт)": "6.16 ( - )",
                     "Эффективен для помещений площадью до": "61.6"},
              cost=Decimal("64490"), stock=7, photos=[], series="DAIJIN")
    cap = render_caption(o, 84990, CFG)
    assert "21000 BTU" in cap and "6.16 кВт" in cap and "до 61.6 м²" in cap


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
