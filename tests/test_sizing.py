from content_factory.content.sizing import size_from_btu


def test_plain_ksize():
    assert size_from_btu(7, category_id=2) == 7
    assert size_from_btu(12, category_id=2) == 12


def test_area_map_residential():
    assert size_from_btu(25, category_id=2) == 7    # площадь 25 м² → 7
    assert size_from_btu(50, category_id=2) == 18
    assert size_from_btu(70, category_id=7) == 24


def test_semi_industrial_keeps_60():
    assert size_from_btu(60, category_id=6) == 60   # полупром: 60 = реальные kBTU


def test_full_btu_divided():
    assert size_from_btu(9000, category_id=2) == 9


def test_garbage_none():
    assert size_from_btu(0, category_id=2) is None
    assert size_from_btu(None, category_id=2) is None
