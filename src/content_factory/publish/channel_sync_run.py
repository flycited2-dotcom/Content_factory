"""CLI «живого канала» (таймер cf-channel-sync, раз в день до утреннего окна):
сверить опубликованные посты с каталогом — продано/цена/оживление — и поправить
подписи в канале (editMessageCaption, троттлинг). Первая сверка после включения
только запоминает цены (baseline), посты не трогает.

  python -m content_factory.publish.channel_sync_run
"""
from __future__ import annotations
import time
from pathlib import Path
from decouple import config

from content_factory.config import load_config
from content_factory.ingest import collect_offers
from content_factory.ingest.oasis_db import fetch_raw_products
from content_factory.catalog.series import group_by_series
from content_factory.content.render import render_caption
from content_factory.pricing.pricing import compute_price
from content_factory.publish.telegram import PublishState, edit_caption
from content_factory.publish.channel_sync import plan_sync


def main():
    cfg = load_config(Path("config/config.yaml"))
    sync_cfg = cfg.channel_sync
    if not sync_cfg.get("enabled"):
        print("channel-sync: выключен (channel_sync.enabled)")
        return
    ps = PublishState(cfg.state.db)
    records = ps.records()
    if not records:
        print("channel-sync: опубликованных постов нет")
        return

    dsn = {"host": config("DB_HOST", "localhost"), "port": config("DB_PORT", "5432"),
           "dbname": config("DB_NAME"), "user": config("DB_USER"), "password": config("DB_PASSWORD")}
    raw = fetch_raw_products(dsn, cfg.source.warehouse,
                             cfg.source.catalog.report_category_ids,
                             cfg.source.catalog.exclude_title_patterns)
    # опт Бриза тем же лукапом, что планировщик — иначе синк перепишет цены розницей
    from content_factory.ingest.breez import live_base_lookup
    offers = collect_offers(raw, Path(config("JAC_STOCK_JSON", "")), cfg.source.catalog,
                            live_base_lookup())
    groups = group_by_series(offers)

    from content_factory.ingest.breez import fetch_breez_utp_by_nc
    utp_map = fetch_breez_utp_by_nc()
    # наценки из бота — тот же расчёт, что в планировщике, иначе синк перепишет цены
    from content_factory.pricing.overrides import apply_overrides, markup_overrides
    pricing_cfg = apply_overrides(cfg.pricing, markup_overrides(cfg.state.db))

    def price_fn(g):
        pr = compute_price(g.representative, pricing_cfg)
        return pr.price if pr.ok else None

    def caption_fn(g, price):
        utp_raw = None
        if g.source == "breeze":
            utp_raw = utp_map.get(g.representative.supplier_sku.split(":", 1)[-1])
        member_prices = []
        for m in g.members:
            if (m.stock or 0) > 0:
                pr = compute_price(m, pricing_cfg)
                member_prices.append((m, pr.price if pr.ok else None))
        return render_caption(g, price, cfg.content, utp_raw=utp_raw,
                              member_prices=member_prices)

    actions, baseline = plan_sync(records, groups, price_fn, caption_fn,
                                  default_channel=config("TELEGRAM_CHANNEL_ID",
                                                         cfg.telegram.channel_id),
                                  min_price_delta=int(sync_cfg.get("min_price_delta", 100)))
    for key, price in baseline:
        ps.update_sync(key, price=price)

    token = config("TELEGRAM_BOT_TOKEN", "")
    pause = float(sync_cfg.get("edit_pause_sec", 4))
    done = {"sold": 0, "reprice": 0, "revive": 0}
    errors = 0
    for a in actions:
        ok, err, gone = edit_caption(token, a.channel, a.message_id, a.caption,
                                     parse_mode=cfg.telegram.parse_mode, retries=2)
        if ok:
            ps.update_sync(a.key, status=("sold" if a.kind == "sold" else "active"),
                           price=a.price, caption=a.caption)
            done[a.kind] += 1
        elif gone:                       # пост удалили руками — больше не трогаем
            ps.update_sync(a.key, status="sold")
        else:
            errors += 1
            print(f"  ошибка правки {a.key}: {err}")
        time.sleep(pause)                # анти-flood Telegram (~20 правок/мин)

    print(f"channel-sync: постов {len(records)} | baseline {len(baseline)} | "
          f"продано {done['sold']} | цены {done['reprice']} | ожило {done['revive']} | "
          f"ошибок {errors}")


if __name__ == "__main__":
    main()
