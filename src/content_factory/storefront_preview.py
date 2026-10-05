"""Безопасный read-only предпросмотр каталога витрины без публикации и записи state."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from content_factory.config import load_config
from content_factory.content.render import render_caption
from content_factory.ingest.storefront_api import collect_storefront_offers
from content_factory.pricing.pricing import compute_price


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/stabilizers-b2c.yaml")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--show-caption", action="store_true")
    args = parser.parse_args(argv)
    cfg = load_config(Path(args.config))
    token = os.environ.get(cfg.source.token_env, "")
    offers = collect_storefront_offers(cfg.source, token)
    print(f"storefront preview: {len(offers)} уникальных доступных товаров")
    for offer in offers[:max(0, args.limit)]:
        priced = compute_price(offer, cfg.pricing)
        print(f"- {offer.brand} {offer.model} | {priced.price or '-'} руб. | "
              f"фото={len(offer.photos)} | {offer.supplier_sku}")
        if args.show_caption:
            print(render_caption(offer, priced.price, cfg.content))
            print()


if __name__ == "__main__":
    main()
