"""Интеграция обвязки: build_context (scheduler_run) с мок-Telegram прогоняет слот."""
from decimal import Decimal
import httpx
from content_factory.config import load_config
from content_factory.models import Offer
from content_factory.catalog.series import group_by_series
from content_factory.content.cards import card_key
from content_factory.publish.telegram import PublishState
from content_factory.orchestrator.confirm_store import ConfirmStore
from content_factory.orchestrator.queue import TaskQueue
from content_factory.orchestrator.tasks import Task
from content_factory.orchestrator.scheduler import run_slot
from content_factory.orchestrator.scheduler_run import build_context


CFG_YAML = (
    "source: {warehouse: Симферополь, categories: [2]}\n"
    "pricing: {default_markup_pct: 5}\n"
    "content: {caption_max: 1024}\n"
    "cards: {dir: '%CARDS%', require_for_publish: true, default_mode: mcp}\n"
    "telegram: {channel_id: '@chan', parse_mode: HTML}\n"
    "review: {price_min: 1000, price_max: 1000000000, require_specs: true, require_card: true}\n"
    "state: {db: '%STATEDB%'}\n"
)


def _setup(tmp_path):
    cards = tmp_path / "cards"
    cards.mkdir()
    body = (CFG_YAML.replace("%CARDS%", str(cards).replace("\\", "/"))
            .replace("%STATEDB%", str(tmp_path / "state.db").replace("\\", "/")))
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text(body, encoding="utf-8")
    cfg = load_config(cfgp)
    o = Offer(supplier_sku="breeze:NC1", source="breeze", brand="Ballu", model="Olympio",
              category_id=2, btu_calc=9, attrs={"Холод, кВт": "2.6"}, cost=Decimal("20000"),
              stock=1, photos=["p"], series="Olympio")
    groups = group_by_series([o])
    (cards / f"{card_key(o.supplier_sku)}.jpg").write_bytes(b"IMG")
    return cfg, groups


def _http(calls):
    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.telegram.org")


def test_wiring_auto_publishes_to_channel(tmp_path):
    cfg, groups = _setup(tmp_path)
    calls = []
    ps, cs = PublishState(cfg.state.db), ConfirmStore(cfg.state.db)
    ctx = build_context(cfg, "TOK", "999", ps, cs, http=_http(calls))
    q = TaskQueue(tmp_path / "q.db")
    q.add(Task(id="t", filter={}, count=5))
    out = run_slot(q.due("2999-01-01 00:00")[0], groups, ctx)
    assert groups[0].key in out.published
    assert any("sendPhoto" in p for p in calls)
    assert ps.is_published(groups[0].key)


def test_wiring_confirm_sends_preview_and_holds(tmp_path):
    cfg, groups = _setup(tmp_path)
    calls = []
    ps, cs = PublishState(cfg.state.db), ConfirmStore(cfg.state.db)
    ctx = build_context(cfg, "TOK", "999", ps, cs, http=_http(calls))
    q = TaskQueue(tmp_path / "q.db")
    q.add(Task(id="t", filter={}, count=5, confirm=True))
    out = run_slot(q.due("2999-01-01 00:00")[0], groups, ctx)
    assert groups[0].key in out.awaiting
    a = cs.get(groups[0].key)
    assert a is not None and a.status == "pending"
    assert not ps.is_published(groups[0].key)      # в канал НЕ ушло — ждёт /approve
