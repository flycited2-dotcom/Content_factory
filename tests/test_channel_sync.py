"""«Живой канал»: чистая логика сверки постов с каталогом (без сети/state)."""
from decimal import Decimal
from content_factory.models import Offer
from content_factory.catalog.series import group_by_series
from content_factory.publish.channel_sync import plan_sync, SOLD_MARK
from content_factory.publish.telegram import PublishedRec


def _rec(key, price=None, status="active", caption="old cap", mid=10):
    return PublishedRec(key=key, message_id=mid, channel="@c", price=price,
                        status=status, caption=caption, ts=0)


def _groups(stock=5):
    o = Offer(supplier_sku="breeze:NC1", source="breeze", brand="Ballu", model="Olympio",
              category_id=2, btu_calc=9, attrs={}, cost=Decimal("20000"),
              stock=stock, photos=[], series="Olympio")
    return group_by_series([o])


KEY = "breeze|ballu|olympio"
PRICE_FN = lambda g: 20000
CAPTION_FN = lambda g, price: f"cap {price}"


def _plan(records, groups, delta=100):
    return plan_sync(records, groups, PRICE_FN, CAPTION_FN, "@def", min_price_delta=delta)


def test_sold_when_out_of_stock():
    actions, baseline = _plan([_rec(KEY, price=20000)], _groups(stock=0))
    (a,) = actions
    assert (a.kind, a.key, a.channel) == ("sold", KEY, "@c")
    assert a.caption == f"{SOLD_MARK}\n\nold cap"
    assert baseline == []


def test_sold_when_series_gone():
    actions, _ = _plan([_rec(KEY, price=20000)], [])
    assert actions[0].kind == "sold"


def test_sold_without_saved_caption():
    actions, _ = _plan([_rec(KEY, price=20000, caption=None)], _groups(stock=0))
    assert actions[0].caption == SOLD_MARK


def test_reprice_when_delta_reached():
    actions, _ = _plan([_rec(KEY, price=18000)], _groups())
    (a,) = actions
    assert (a.kind, a.caption, a.price) == ("reprice", "cap 20000", 20000)


def test_no_reprice_below_delta():
    actions, baseline = _plan([_rec(KEY, price=19950)], _groups())
    assert actions == [] and baseline == []


def test_baseline_price_written_without_edit():
    actions, baseline = _plan([_rec(KEY, price=None)], _groups())
    assert actions == []
    assert baseline == [(KEY, 20000)]


def test_revive_when_back_in_stock():
    actions, _ = _plan([_rec(KEY, price=20000, status="sold")], _groups(stock=3))
    (a,) = actions
    assert (a.kind, a.caption, a.price) == ("revive", "cap 20000", 20000)


def test_sold_stays_sold():
    actions, baseline = _plan([_rec(KEY, price=20000, status="sold")], _groups(stock=0))
    assert actions == [] and baseline == []


def test_excel_keys_skipped():
    actions, baseline = _plan([_rec("excel|stinol|sts 167", price=20000)], [])
    assert actions == [] and baseline == []


def test_default_channel_used_when_empty():
    rec = _rec(KEY, price=18000)
    rec.channel = ""
    actions, _ = _plan([rec], _groups())
    assert actions[0].channel == "@def"
