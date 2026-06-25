from decimal import Decimal
from content_factory.models import Offer
from content_factory.content.cards import card_key, resolve_photos, CardConfig


def _o(sku, photos):
    return Offer(supplier_sku=sku, source="s", brand="B", model="M", category_id=2,
                 btu_calc=7, attrs={}, cost=Decimal("1"), retail_ref=None, stock=1,
                 photos=photos, series=None, content_hash="h")


def test_card_key_sanitizes():
    assert card_key("rusklimat:NC-7/9") == "NC-7_9"
    assert card_key("jac:MDV AB 07") == "MDV_AB_07"
    assert card_key("rusklimat:НК-1478151") == "НК-1478151"   # кириллица сохраняется


def test_resolve_url_percent_encodes_cyrillic(tmp_path):
    (tmp_path / "НК-1478151.jpg").write_bytes(b"img")
    cfg = CardConfig(enabled=True, dir=str(tmp_path), base_url="https://x/c", exts=[".jpg"])
    o = _o("rusklimat:НК-1478151", ["https://supplier/p.jpg"])
    assert resolve_photos(o, cfg) == ["https://x/c/%D0%9D%D0%9A-1478151.jpg"]


def test_resolve_uses_supplier_photo_when_no_card(tmp_path):
    cfg = CardConfig(enabled=True, dir=str(tmp_path), base_url="https://x/c", exts=[".jpg"])
    o = _o("rusklimat:NC7", ["https://supplier/p.jpg"])
    assert resolve_photos(o, cfg) == ["https://supplier/p.jpg"]


def test_resolve_uses_card_when_present(tmp_path):
    (tmp_path / "NC7.jpg").write_bytes(b"img")
    cfg = CardConfig(enabled=True, dir=str(tmp_path), base_url="https://x/c/", exts=[".jpg"])
    o = _o("rusklimat:NC7", ["https://supplier/p.jpg"])
    assert resolve_photos(o, cfg) == ["https://x/c/NC7.jpg"]   # карточка вместо фото поставщика


def test_resolve_disabled_returns_supplier(tmp_path):
    (tmp_path / "NC7.jpg").write_bytes(b"img")
    cfg = CardConfig(enabled=False, dir=str(tmp_path), base_url="https://x/c", exts=[".jpg"])
    o = _o("rusklimat:NC7", ["https://supplier/p.jpg"])
    assert resolve_photos(o, cfg) == ["https://supplier/p.jpg"]
