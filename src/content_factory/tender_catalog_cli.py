"""Одноразовый read-only JSON CLI для каталогов владельца по SSH."""
from __future__ import annotations

import argparse
import base64
import json

from content_factory.tender_mcp import (
    build_search_response,
    climate_catalog_status,
    load_live_offers,
)
from content_factory.private_price_catalog import (
    prices_dir_from_config,
    private_price_catalog_status,
    search_private_price_catalog,
)


def decode_query(value: str) -> str:
    try:
        return base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("query-base64 должен содержать корректный UTF-8") from exc


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    search = subparsers.add_parser("search")
    search.add_argument("--query-base64", required=True)
    search.add_argument("--limit", type=int, default=20)
    subparsers.add_parser("status")
    price_search = subparsers.add_parser("search-prices")
    price_search.add_argument("--query-base64", required=True)
    price_search.add_argument("--limit", type=int, default=20)
    subparsers.add_parser("prices-status")
    args = parser.parse_args()

    if args.command == "status":
        payload = climate_catalog_status(force_refresh=True)
    elif args.command == "prices-status":
        payload = private_price_catalog_status(prices_dir_from_config())
    elif args.command == "search-prices":
        query = decode_query(args.query_base64)
        payload = search_private_price_catalog(prices_dir_from_config(), query, limit=args.limit)
    else:
        query = decode_query(args.query_base64)
        payload = build_search_response(load_live_offers(), query, limit=args.limit)
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
