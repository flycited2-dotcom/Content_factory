from content_factory.config import ReviewConfig
from content_factory.review.rules import review, ReviewItem


def _item(tmp_path, **kw):
    card = tmp_path / "card.jpg"
    card.write_bytes(b"img")
    base = dict(price=25990, caption="Ballu Olympio — 9000 BTU\nЦена: 25 990 ₽",
                attrs={"Холод, кВт": "2.6"}, card_path=str(card),
                brand="Ballu", category_id=2)
    base.update(kw)
    return ReviewItem(**base)


CFG = ReviewConfig(price_min=1000, price_max=1_000_000, require_specs=True,
                   require_card=True, caption_max=1024)


def test_all_ok_passes(tmp_path):
    ok, reasons = review(_item(tmp_path), CFG)
    assert ok and reasons == []


def test_price_zero_fails(tmp_path):
    ok, reasons = review(_item(tmp_path, price=0), CFG)
    assert not ok and any("цена" in r.lower() for r in reasons)


def test_price_out_of_bounds_fails(tmp_path):
    ok, reasons = review(_item(tmp_path, price=10), CFG)        # < price_min
    assert not ok and any("миниму" in r.lower() for r in reasons)
    ok, reasons = review(_item(tmp_path, price=5_000_000), CFG)  # > price_max
    assert not ok and any("максиму" in r.lower() for r in reasons)


def test_no_specs_fails_when_required(tmp_path):
    ok, reasons = review(_item(tmp_path, attrs={"Холод, кВт": ""}), CFG)
    assert not ok and any("ттх" in r.lower() for r in reasons)


def test_specs_not_required_passes(tmp_path):
    cfg = ReviewConfig(price_min=1000, price_max=1_000_000, require_specs=False,
                       require_card=True, caption_max=1024)
    ok, reasons = review(_item(tmp_path, attrs={}), cfg)
    assert ok and reasons == []


def test_missing_card_fails(tmp_path):
    ok, reasons = review(_item(tmp_path, card_path=str(tmp_path / "nope.jpg")), CFG)
    assert not ok and any("карточ" in r.lower() for r in reasons)


def test_empty_card_file_fails(tmp_path):
    empty = tmp_path / "empty.jpg"
    empty.write_bytes(b"")
    ok, reasons = review(_item(tmp_path, card_path=str(empty)), CFG)
    assert not ok and any("карточ" in r.lower() for r in reasons)


def test_card_not_required_passes(tmp_path):
    cfg = ReviewConfig(price_min=1000, price_max=1_000_000, require_specs=True,
                       require_card=False, caption_max=1024)
    ok, reasons = review(_item(tmp_path, card_path=None), cfg)
    assert ok and reasons == []


def test_empty_caption_fails(tmp_path):
    ok, reasons = review(_item(tmp_path, caption="   "), CFG)
    assert not ok and any("описание" in r.lower() for r in reasons)


def test_caption_too_long_fails(tmp_path):
    cfg = ReviewConfig(price_min=1000, price_max=1_000_000, require_specs=True,
                       require_card=True, caption_max=50)
    ok, reasons = review(_item(tmp_path, caption="X" * 100), cfg)
    assert not ok and any("лимит" in r.lower() for r in reasons)


def test_stopword_in_caption_fails(tmp_path):
    ok, reasons = review(_item(tmp_path, caption="Ballu Olympio. Звоните срочно!"), CFG,
                         stop_words=["звоните"])
    assert not ok and any("стоп" in r.lower() for r in reasons)


def test_missing_brand_or_type_fails(tmp_path):
    ok, reasons = review(_item(tmp_path, brand=""), CFG)
    assert not ok and any("бренд" in r.lower() for r in reasons)
    ok, reasons = review(_item(tmp_path, category_id=None), CFG)
    assert not ok and any("тип" in r.lower() for r in reasons)


def test_multiple_reasons_collected(tmp_path):
    ok, reasons = review(_item(tmp_path, price=0, brand="", attrs={}), CFG)
    assert not ok and len(reasons) >= 3
