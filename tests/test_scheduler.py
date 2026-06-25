from decimal import Decimal
from content_factory.models import Offer
from content_factory.config import ContentConfig, ReviewConfig
from content_factory.pricing.pricing import PricingConfig
from content_factory.catalog.series import group_by_series
from content_factory.content.cards import card_key
from content_factory.publish.telegram import PublishResult
from content_factory.orchestrator.queue import TaskQueue
from content_factory.orchestrator.tasks import Task
from content_factory.orchestrator import scheduler as S


def _o(sku, source="breeze", series="Olympio", cat=2, btu=9, attrs=None):
    return Offer(supplier_sku=sku, source=source, brand="Ballu", model=series,
                 category_id=cat, btu_calc=btu,
                 attrs={"Холод, кВт": "2.6"} if attrs is None else attrs,
                 cost=Decimal("20000"), stock=1, photos=["p"], series=series)


def _ctx(tmp_path, **kw):
    rec = dict(published=[], submitted=[], alerts=[], confirms=[], pubkeys=set())

    def publish(group, card, caption):
        rec["published"].append((group.key, card, caption))
        return PublishResult(ok=True, message_id=1)

    def submit_cards(groups, mode):
        rec["submitted"].append(([g.key for g in groups], mode))

    def alert(group, reasons):
        rec["alerts"].append((group.key, reasons))

    def confirm(slot, group, card, caption):
        rec["confirms"].append((group.key, card, caption))

    ctx = S.PipelineContext(
        cards_dir=str(tmp_path / "cards"),
        pricing_cfg=PricingConfig(default_markup_pct=5),
        content_cfg=ContentConfig(caption_max=1024, stop_words=[], descriptions={}),
        review_cfg=ReviewConfig(price_min=1000, price_max=1_000_000_000,
                                require_specs=True, require_card=True, caption_max=1024),
        stop_words=[], require_card=True, default_mode="mcp",
        published_keys=lambda: rec["pubkeys"],
        publish=publish, submit_cards=submit_cards, alert=alert, confirm=confirm)
    for k, v in kw.items():
        setattr(ctx, k, v)
    return ctx, rec


def _make_card(tmp_path, group):
    d = tmp_path / "cards"
    d.mkdir(exist_ok=True)
    f = d / f"{card_key(group.supplier_sku)}.jpg"
    f.write_bytes(b"IMG")
    return f


def test_publishes_series_with_ready_card(tmp_path):
    groups = group_by_series([_o("breeze:NC1")])
    _make_card(tmp_path, groups[0])
    ctx, rec = _ctx(tmp_path)
    slot = TaskQueue(tmp_path / "q.db"); slot.add(Task(id="t", filter={}, count=5))
    s = slot.due("2999-01-01 00:00")[0]
    out = S.run_slot(s, groups, ctx)
    assert groups[0].key in out.published
    assert len(rec["published"]) == 1
    assert "Ballu" in rec["published"][0][2]      # подпись содержит бренд


def test_require_card_gate_defers_and_submits(tmp_path):
    groups = group_by_series([_o("breeze:NC1")])   # карточки нет на диске
    ctx, rec = _ctx(tmp_path)
    out = S.run_slot(_slot(tmp_path, count=5), groups, ctx)
    assert groups[0].key in out.submitted
    assert out.published == []
    assert rec["published"] == []
    assert rec["submitted"] and rec["submitted"][0][1] == "mcp"   # отправлено в фотоагент с режимом


def test_review_fail_holds_and_alerts(tmp_path):
    groups = group_by_series([_o("breeze:NC1", attrs={})])   # нет ТТХ → ревизия валит
    _make_card(tmp_path, groups[0])
    ctx, rec = _ctx(tmp_path)
    out = S.run_slot(_slot(tmp_path), groups, ctx)
    assert out.published == []
    assert any(k == groups[0].key for k, _ in out.held)
    assert rec["alerts"] and rec["alerts"][0][0] == groups[0].key


def test_confirm_flag_holds_for_approval(tmp_path):
    groups = group_by_series([_o("breeze:NC1")])
    _make_card(tmp_path, groups[0])
    ctx, rec = _ctx(tmp_path)
    out = S.run_slot(_slot(tmp_path, confirm=True), groups, ctx)
    assert groups[0].key in out.awaiting
    assert rec["confirms"] and rec["published"] == []          # не публикуем без OK


def test_anti_dup_skips_already_published(tmp_path):
    groups = group_by_series([_o("breeze:NC1"), _o("daichi:D1", source="daichi", series="Peak")])
    for g in groups:
        _make_card(tmp_path, g)
    ctx, rec = _ctx(tmp_path)
    rec["pubkeys"].add(groups[0].key)                          # первый уже опубликован
    out = S.run_slot(_slot(tmp_path, count=10), groups, ctx)
    assert groups[0].key not in out.published
    assert groups[1].key in out.published


def test_run_due_marks_slot_done(tmp_path):
    groups = group_by_series([_o("breeze:NC1")])
    _make_card(tmp_path, groups[0])
    ctx, rec = _ctx(tmp_path)
    q = TaskQueue(tmp_path / "q.db")
    q.add(Task(id="t", filter={}, count=5, schedule=["2026-06-26 10:00"]))
    res1 = S.run_due("2026-06-26 11:00", q, groups, ctx)
    assert len(res1) == 1
    res2 = S.run_due("2026-06-26 12:00", q, groups, ctx)        # слот уже done
    assert res2 == []


def _slot(tmp_path, count=5, confirm=False):
    q = TaskQueue(tmp_path / f"q{count}{confirm}.db")
    q.add(Task(id="t", filter={}, count=count, confirm=confirm))
    return q.due("2999-01-01 00:00")[0]
