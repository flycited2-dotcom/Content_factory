from decimal import Decimal
from content_factory.models import Offer
from content_factory.content.cards import (
    card_key, resolve_photos, CardConfig, mode_for, build_modes_map)
from content_factory.catalog.series import group_by_series


def _o(sku, photos, cat=2):
    return Offer(supplier_sku=sku, source="s", brand="B", model="M", category_id=cat,
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


# ── авто-выбор mode по категории (детерминированно, без ИИ) ───────────────────
def test_mode_for_known_category():
    modes = {2: "mcp", 99: "kbt"}
    assert mode_for(_o("s:1", ["p"], cat=99), modes, "mcp") == "kbt"


def test_mode_for_unknown_category_falls_back_to_default():
    assert mode_for(_o("s:1", ["p"], cat=123), {2: "mcp"}, "mcp") == "mcp"


def test_mode_for_empty_map_returns_default():
    assert mode_for(_o("s:1", ["p"], cat=2), {}, "mcp") == "mcp"
    assert mode_for(_o("s:1", ["p"], cat=2), None, "mcp") == "mcp"


def test_mode_for_missing_category_id_returns_default():
    assert mode_for(_o("s:1", ["p"], cat=None), {2: "mcp"}, "mcp") == "mcp"


def test_build_modes_map_auto_by_category():
    g = group_by_series([_o("breeze:NC7", ["p"], cat=7)])[0]
    modes, unknown = build_modes_map([g], {7: "kbt"}, "mcp")
    assert modes[g.key] == "kbt"
    assert unknown == set()


def test_build_modes_map_override_wins():
    g = group_by_series([_o("breeze:NC7", ["p"], cat=7)])[0]
    modes, _ = build_modes_map([g], {7: "kbt"}, "mcp", overrides={g.key: "special"})
    assert modes[g.key] == "special"          # ручной per-series override — высший приоритет


def test_build_modes_map_unknown_category_default_and_flagged():
    g = group_by_series([_o("breeze:NC9", ["p"], cat=123)])[0]
    modes, unknown = build_modes_map([g], {7: "kbt"}, "mcp")
    assert modes[g.key] == "mcp"              # безопасный дефолт
    assert unknown == {123}                   # и помечена для предупреждения владельцу


def test_build_modes_map_empty_map_no_alert():
    g = group_by_series([_o("breeze:NC2", ["p"], cat=2)])[0]
    modes, unknown = build_modes_map([g], {}, "mcp")
    assert modes[g.key] == "mcp"
    assert unknown == set()                   # карта не настроена → не алертим
