from decimal import Decimal
from content_factory.models import Offer
from content_factory.catalog.series import group_by_series, series_key


def _o(sku, brand, series, btu, model=None):
    return Offer(supplier_sku=sku, source="breeze", brand=brand, model=model or f"{series} {btu}",
                 category_id=2, btu_calc=btu, attrs={}, cost=Decimal("1"), retail_ref=None,
                 stock=1, photos=[], series=series, content_hash="h")


def test_groups_models_of_same_series():
    offers = [_o("b:3", "Ballu", "Olympio", 12), _o("b:1", "Ballu", "Olympio", 7),
              _o("b:2", "Ballu", "Olympio", 9)]
    groups = group_by_series(offers)
    assert len(groups) == 1
    g = groups[0]
    assert [m.btu_calc for m in g.members] == [7, 9, 12]      # сортировка по размеру
    assert g.representative.supplier_sku == "b:1"             # младший размер
    assert g.supplier_sku == "b:1"


def test_distinct_series_separate_groups():
    offers = [_o("b:1", "Ballu", "Olympio", 7), _o("d:1", "Daikin", "FTXB", 9)]
    assert len(group_by_series(offers)) == 2


def test_no_series_each_its_own_group():
    a = _o("x:1", "Ballu", "", 7, model="ABC")
    b = _o("x:2", "Ballu", "", 9, model="DEF")
    a.series = None
    b.series = None
    groups = group_by_series([a, b])
    assert len(groups) == 2


def test_series_key_case_insensitive():
    assert series_key(_o("b:1", "Ballu", "Olympio", 7)) == series_key(_o("b:2", "ballu", "olympio", 9))


def test_merges_refrigerant_variants():
    # Paramount и Paramount R32 — близнецы → одна серия с именем без хладагента.
    groups = group_by_series([_o("m:1", "Midea", "Paramount", 7),
                              _o("m:2", "Midea", "Paramount R32", 9),
                              _o("m:3", "Midea", "Paramount (Inverter)", 12)])
    assert len(groups) == 1
    assert groups[0].series == "Paramount"
    assert [m.btu_calc for m in groups[0].members] == [7, 9, 12]


def test_does_not_merge_distinct_series():
    groups = group_by_series([_o("m:1", "Midea", "Paramount", 7),
                              _o("m:2", "Midea", "Aurora", 9)])
    assert len(groups) == 2
