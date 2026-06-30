from decimal import Decimal
from content_factory.models import Offer
from content_factory.catalog.series import group_by_series
from content_factory.orchestrator.tasks import Task, matches, select_items


def _o(sku, source="breeze", brand="Ballu", series="Olympio", cat=2, btu=9):
    return Offer(supplier_sku=sku, source=source, brand=brand, model=series,
                 category_id=cat, btu_calc=btu, attrs={"Холод, кВт": "2.6"},
                 cost=Decimal("20000"), stock=1, photos=["p"], series=series)


def _groups():
    return group_by_series([
        _o("breeze:NC1", source="breeze", series="Olympio", cat=2),
        _o("daichi:D1", source="daichi", series="Peak", cat=2),
        _o("breeze:NC2", source="breeze", series="Gloria", cat=6),
    ])


def test_task_defaults():
    t = Task(id="t1", filter={"categories": [2]}, count=5)
    assert t.mode == "mcp" and t.confirm is False and t.channel == "" and t.schedule == []


def test_matches_by_category_and_source():
    g = _groups()
    breeze_cat2 = next(x for x in g if x.source == "breeze" and x.category_id == 2)
    assert matches(breeze_cat2, {"categories": [2]})
    assert matches(breeze_cat2, {"source": "breeze"})
    assert not matches(breeze_cat2, {"categories": [6]})
    assert not matches(breeze_cat2, {"source": "daichi"})


def test_matches_empty_filter_matches_all():
    g = _groups()[0]
    assert matches(g, {})


def test_matches_series_whitelist():
    g = _groups()
    target = g[0]
    assert matches(target, {"series_whitelist": [target.key]})
    assert not matches(target, {"series_whitelist": ["nope|x|y"]})


def test_select_takes_n_unpublished():
    g = _groups()
    sel = select_items(g, {"categories": [2]}, published_keys=set(), count=1)
    assert len(sel) == 1 and sel[0].category_id == 2


def test_select_skips_published():
    g = _groups()
    cat2 = [x for x in g if x.category_id == 2]
    published = {cat2[0].key}
    sel = select_items(g, {"categories": [2]}, published_keys=published, count=10)
    keys = {x.key for x in sel}
    assert cat2[0].key not in keys
    assert cat2[1].key in keys


def test_select_respects_count_cap():
    g = _groups()
    sel = select_items(g, {}, published_keys=set(), count=2)
    assert len(sel) == 2


def test_select_skips_out_of_stock():
    o = _o("breeze:NS1", source="breeze", series="Zero", cat=2)
    o.stock = 0                                   # нет в наличии
    groups = group_by_series([o])
    sel = select_items(groups, {}, published_keys=set(), count=10)
    assert sel == []                              # серия без остатка не выбирается
