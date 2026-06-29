"""CLI планировщика (таймер `cf-scheduler`): загрузить план(ы) в очередь, собрать каталог из
oasis, и на каждый дозревший слот провести серии через конвейер (цена→подпись→ревизия→
confirm/публикация). Карточки генерит ОТДЕЛЬНЫЙ таймер `cards_run` (мост к фотоагенту) —
здесь серии без готовой карточки просто откладываются до следующего окна.

  python -m content_factory.orchestrator.scheduler_run
"""
from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path
from decouple import config

from content_factory.config import load_config
from content_factory.ingest import collect_offers
from content_factory.ingest.oasis_db import fetch_raw_products
from content_factory.catalog.series import group_by_series
from content_factory.publish.telegram import publish_post, send_message, PublishState
from content_factory.orchestrator.queue import TaskQueue
from content_factory.orchestrator.confirm_store import ConfirmStore
from content_factory.orchestrator.plans import load_plans_into_queue
from content_factory.orchestrator.scheduler import PipelineContext, run_due


def build_context(cfg, token: str, owner_chat: str, pub_state: PublishState,
                  confirm_store: ConfirmStore, http=None, channel_id: str = "") -> PipelineContext:
    """Собрать PipelineContext с реальными действиями (Telegram/state).
    channel_id — боевой канал из .env (секрет, не из yaml); fallback — cfg.telegram.channel_id."""
    chan = channel_id or cfg.telegram.channel_id

    def publish(group, card, caption):
        return publish_post(token, chan, card, caption,
                            http=http, parse_mode=cfg.telegram.parse_mode,
                            key=group.key, state=pub_state, retries=2)

    def submit_cards(groups, mode):
        # карточки добирает отдельный таймер cards_run; здесь только лог
        print(f"  отложено (нет карточки): {len(groups)} серий, режим {mode}")

    def alert(group, reasons):
        if token and owner_chat:
            send_message(token, owner_chat,
                         f"⚠️ {group.brand} {group.series}: {'; '.join(reasons)}", http=http)

    def confirm(slot, group, card, caption):
        channel = slot.channel or chan
        confirm_store.add(group.key, channel, card, caption)
        if not (token and owner_chat):
            return
        # Превью владельцу с inline-кнопками ✅/❌ (тап вместо печати ключа).
        ad, rd = f"approve:{group.key}", f"reject:{group.key}"
        if len(ad.encode()) <= 64 and len(rd.encode()) <= 64:   # лимит callback_data Telegram
            kb = json.dumps({"inline_keyboard": [[
                {"text": "✅ Опубликовать", "callback_data": ad},
                {"text": "❌ Отклонить", "callback_data": rd}]]}, ensure_ascii=False)
            publish_post(token, owner_chat, card, f"{caption}\n\n— на подтверждение —",
                         http=http, parse_mode=cfg.telegram.parse_mode, reply_markup=kb)
        else:                                                   # длинный ключ → текстовый фолбэк
            preview = f"{caption}\n\n— Подтвердить: /approve {group.key}\n— Отклонить: /reject {group.key}"
            publish_post(token, owner_chat, card, preview, http=http,
                         parse_mode=cfg.telegram.parse_mode)

    return PipelineContext(
        cards_dir=cfg.cards.dir, pricing_cfg=cfg.pricing, content_cfg=cfg.content,
        review_cfg=cfg.review, stop_words=cfg.content.stop_words,
        require_card=cfg.cards.require_for_publish, default_mode=cfg.default_card_mode,
        published_keys=pub_state.published_keys, publish=publish,
        submit_cards=submit_cards, alert=alert, confirm=confirm)


def main():
    cfg = load_config(Path("config/config.yaml"))
    q = TaskQueue(cfg.state.db)
    if Path("tasks").is_dir():
        load_plans_into_queue("tasks", q)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    if not q.due(now):
        print(f"scheduler: дозревших слотов нет ({now})")
        return

    # каталог нужен только если есть что исполнять
    dsn = {"host": config("DB_HOST", "localhost"), "port": config("DB_PORT", "5432"),
           "dbname": config("DB_NAME"), "user": config("DB_USER"), "password": config("DB_PASSWORD")}
    raw = fetch_raw_products(dsn, cfg.source.warehouse,
                             cfg.source.catalog.report_category_ids,
                             cfg.source.catalog.exclude_title_patterns)
    offers = collect_offers(raw, Path(config("JAC_STOCK_JSON", "")), cfg.source.catalog,
                            lambda nc: None)
    groups = group_by_series(offers)

    ctx = build_context(cfg, token=config("TELEGRAM_BOT_TOKEN", ""),
                        owner_chat=config("TELEGRAM_OWNER_CHAT_ID", config("FOTOGEN_CHAT_ID", "")),
                        pub_state=PublishState(cfg.state.db),
                        confirm_store=ConfirmStore(cfg.state.db),
                        channel_id=config("TELEGRAM_CHANNEL_ID", ""))
    outcomes = run_due(now, q, groups, ctx)
    pub = sum(len(o.published) for _, o in outcomes)
    awe = sum(len(o.awaiting) for _, o in outcomes)
    held = sum(len(o.held) for _, o in outcomes)
    sub = sum(len(o.submitted) for _, o in outcomes)
    print(f"scheduler {now}: слотов {len(outcomes)} | опубликовано {pub} | "
          f"на подтверждении {awe} | held {held} | отложено(нет карточки) {sub}")


if __name__ == "__main__":
    main()
