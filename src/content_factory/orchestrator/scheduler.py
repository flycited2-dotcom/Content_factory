"""Планировщик: исполняет дозревшие слоты задач. На каждый слот — выбрать N невыпущенных
серий и провести через конвейер (двухфазно, как в avito-bridge):
  • нет готовой карточки (require_card) → отложить + поставить в очередь фотоагента (submit);
  • карточка готова → цена → подпись → ревизия → публикация (или held+алерт при fail);
  • slot.confirm → не публикуем, ставим в очередь подтверждения владельца (пилот).
Не хватило созревших карточек к слоту — публикуем сколько готово, остальное берёт следующий
слот (анти-дубль по published_keys). Слот исполняется один раз (mark_done)."""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path

from content_factory.content.cards import card_key
from content_factory.content.render import render_caption
from content_factory.pricing.pricing import compute_price
from content_factory.review.rules import review, ReviewItem
from content_factory.orchestrator.tasks import select_items


@dataclass
class PipelineContext:
    cards_dir: str
    pricing_cfg: object
    content_cfg: object
    review_cfg: object
    stop_words: list = field(default_factory=list)
    require_card: bool = True
    default_mode: str = "mcp"
    published_keys: object = None     # () -> set[str]
    publish: object = None            # (group, card_path, caption) -> PublishResult
    submit_cards: object = None       # (groups, mode) -> None
    alert: object = None              # (group, reasons) -> None
    confirm: object = None            # (slot, group, card_path, caption) -> None
    utp_lookup: object = None         # (group) -> utp_raw|None (УТП Бриза для подписи)


@dataclass
class SlotOutcome:
    published: list = field(default_factory=list)     # group.key
    held: list = field(default_factory=list)          # (group.key, reasons)
    submitted: list = field(default_factory=list)     # group.key (нет карточки → в фотоагент)
    awaiting: list = field(default_factory=list)      # group.key (ждут подтверждения)


def card_path(cards_dir: str, group, ext: str = ".jpg") -> Path:
    """Ожидаемый путь файла карточки серии (имя = код репрезентативного товара)."""
    return Path(cards_dir) / f"{card_key(group.supplier_sku)}{ext}"


def card_ready(path) -> bool:
    p = Path(path)
    return p.exists() and p.stat().st_size > 0


def _price_of(group, pricing_cfg):
    pr = compute_price(group.representative, pricing_cfg)
    return pr.price if pr.ok else None


def run_slot(slot, groups, ctx: PipelineContext) -> SlotOutcome:
    published = ctx.published_keys() if ctx.published_keys else set()
    selected = select_items(groups, slot.filter, published, slot.count)
    out = SlotOutcome()
    to_submit = []

    for g in selected:
        card = card_path(ctx.cards_dir, g)
        if ctx.require_card and not card_ready(card):
            to_submit.append(g)
            out.submitted.append(g.key)
            continue

        price = _price_of(g, ctx.pricing_cfg)
        utp_raw = ctx.utp_lookup(g) if ctx.utp_lookup else None
        # цены всех членов серии в наличии → серийная подпись («от X ₽» + линейка)
        member_prices = []
        for m in getattr(g, "members", []) or []:
            if (m.stock or 0) > 0:
                pr = compute_price(m, ctx.pricing_cfg)
                member_prices.append((m, pr.price if pr.ok else None))
        caption = render_caption(g, price, ctx.content_cfg, utp_raw=utp_raw,
                                 member_prices=member_prices)
        item = ReviewItem(price=price, caption=caption, attrs=g.representative.attrs,
                          card_path=str(card) if card_ready(card) else None,
                          brand=g.brand, category_id=g.category_id)
        ok, reasons = review(item, ctx.review_cfg, ctx.stop_words)
        if not ok:
            if ctx.alert:
                ctx.alert(g, reasons)
            out.held.append((g.key, reasons))
            continue

        if slot.confirm:                              # пилот: ждём OK владельца
            if ctx.confirm:
                ctx.confirm(slot, g, str(card), caption)
            out.awaiting.append(g.key)
            continue

        res = ctx.publish(g, str(card), caption)
        if res.ok and not res.skipped:
            out.published.append(g.key)
        elif not res.ok:
            if ctx.alert:
                ctx.alert(g, [res.error or "publish failed"])
            out.held.append((g.key, [res.error or "publish failed"]))

    if to_submit and ctx.submit_cards:                # фаза submit: добрать карточки
        ctx.submit_cards(to_submit, slot.mode or ctx.default_mode)
    return out


def run_due(now: str, queue, groups, ctx: PipelineContext) -> list:
    """Исполнить все дозревшие слоты на момент now. Каждый — один раз (mark_done)."""
    outcomes = []
    for slot in queue.due(now):
        outcomes.append((slot, run_slot(slot, groups, ctx)))
        queue.mark_done(slot.task_id, slot.due_at)
    return outcomes
