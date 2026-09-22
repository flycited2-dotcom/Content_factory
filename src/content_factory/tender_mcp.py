"""Read-only MCP-каталог климатической техники для тендерного агента.

Процесс запускается через SSH stdio рядом с БД oasis. Никаких операций заказа,
резерва, публикации или изменения каталога этот модуль не предоставляет.
"""
from __future__ import annotations

import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from decouple import config
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from content_factory.config import load_config
from content_factory.ingest import collect_offers
from content_factory.ingest.breez import live_base_lookup
from content_factory.ingest.oasis_db import fetch_raw_products
from content_factory.models import Offer
from content_factory.private_price_catalog import (
    prices_dir_from_config,
    private_price_catalog_status as _private_price_catalog_status,
    search_private_price_catalog as _search_private_price_catalog,
)


SOURCE_LABELS = {
    "breeze": "Бриз",
    "daichi": "Daichi",
    "rusklimat": "Русклимат",
    "jac": "JAC",
}
MAX_LIMIT = 100
CACHE_TTL_SECONDS = 60
_TOKEN_RE = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)
_GENERIC_TOKENS = {
    "и", "для", "поставка", "поставить", "продажа", "оборудование",
    "климатический", "климатическая", "климатическое", "климатической",
    "кондиционер", "кондиционеры", "кондиционера", "кондиционирование",
    "сплит", "система", "системы", "систему", "монтаж", "установка",
    "демонтаж", "пусконаладка", "шт", "квт", "кв", "м",
}
_cache_lock = threading.Lock()
_cache_at = 0.0
_cache: list[Offer] = []

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

server = MCPServer(
    name="tlt-owner-catalogs",
    title="Приватные каталоги ТЛТ",
    version="0.1.0",
    instructions=(
        "Read-only search over the owner's climate catalog. "
        "Prices and stock are snapshots and must be confirmed before a tender bid."
    ),
)


@server.tool(
    name="private_price_catalog_status",
    title="Проверить частные прайсы поставщиков",
    description=(
        "Показывает доступные приватные прайсы и автоматически выведенные из них "
        "товарные направления. Не читает и не изменяет почту."
    ),
    annotations=READ_ONLY,
)
def private_price_catalog_status() -> dict[str, object]:
    return _private_price_catalog_status(prices_dir_from_config())


@server.tool(
    name="search_private_supplier_prices",
    title="Найти товар в частных прайсах",
    description=(
        "Универсально ищет товар в актуальных XLSX-прайсах всех подключённых поставщиков "
        "без заранее заданного списка товарных категорий. Наличие требует подтверждения."
    ),
    annotations=READ_ONLY,
)
def search_private_supplier_prices(query: str, limit: int = 20) -> dict[str, object]:
    return _search_private_price_catalog(prices_dir_from_config(), query, limit=limit)


def load_live_offers(*, force: bool = False) -> list[Offer]:
    """Получить живой каталог oasis + Бриз + JAC с коротким кэшем процесса."""
    global _cache_at, _cache
    now = time.monotonic()
    with _cache_lock:
        if not force and _cache and now - _cache_at < CACHE_TTL_SECONDS:
            return list(_cache)

        cfg_path = Path(config("CONTENT_FACTORY_CONFIG", "config/config.yaml"))
        cfg = load_config(cfg_path)
        dsn = {
            "host": config("DB_HOST", "localhost"),
            "port": config("DB_PORT", "5432"),
            "dbname": config("DB_NAME"),
            "user": config("DB_USER"),
            "password": config("DB_PASSWORD"),
        }
        raw = fetch_raw_products(
            dsn,
            cfg.source.warehouse,
            cfg.source.catalog.report_category_ids,
            cfg.source.catalog.exclude_title_patterns,
        )
        offers = collect_offers(
            raw,
            Path(config("JAC_STOCK_JSON", "")),
            cfg.source.catalog,
            live_base_lookup(),
        )
        _cache = [offer for offer in offers if offer.stock > 0]
        _cache_at = time.monotonic()
        return list(_cache)


def search_offers(offers: list[Offer], query: str, *, limit: int = 20) -> list[Offer]:
    normalized_query = _normalize(query)
    if not normalized_query:
        raise ValueError("Поисковый запрос не может быть пустым")
    tokens = [token for token in _tokens(normalized_query) if token not in _GENERIC_TOKENS]
    ranked: list[tuple[int, int, float, str, Offer]] = []
    for offer in offers:
        haystack = _normalize(
            " ".join(
                (
                    offer.source,
                    SOURCE_LABELS.get(offer.source.casefold(), offer.source),
                    offer.supplier_sku,
                    offer.brand,
                    offer.model,
                    offer.series or "",
                    " ".join(f"{key} {value}" for key, value in offer.attrs.items()),
                )
            )
        )
        if tokens:
            matched = sum(token in haystack for token in tokens)
            if matched == 0:
                continue
            score = matched * 10 + (25 if all(token in haystack for token in tokens) else 0)
        else:
            score = 1
        if normalized_query in haystack:
            score += 50
        cost = float(offer.cost) if offer.cost is not None else float("inf")
        ranked.append((-score, -offer.stock, cost, haystack, offer))
    ranked.sort(key=lambda row: row[:4])
    safe_limit = max(1, min(int(limit), MAX_LIMIT))
    return [row[4] for row in ranked[:safe_limit]]


def build_search_response(offers: list[Offer], query: str, *, limit: int = 20) -> dict[str, object]:
    products = search_offers(offers, query, limit=limit)
    checked_at = datetime.now(UTC).isoformat()
    return {
        "ok": True,
        "query": " ".join(query.split()),
        "total": len(products),
        "returned": len(products),
        "checkedAt": checked_at,
        "sources": sorted({offer.source for offer in products}),
        "products": [_product_payload(offer, checked_at) for offer in products],
        "notice": "Цена и остаток актуальны на момент проверки и требуют подтверждения перед заявкой.",
    }


@server.tool(
    name="climate_catalog_status",
    title="Проверить климатический каталог",
    description="Проверяет доступ к живому каталогу и показывает количество товаров по источникам.",
    annotations=READ_ONLY,
)
def climate_catalog_status(force_refresh: bool = False) -> dict[str, object]:
    offers = load_live_offers(force=force_refresh)
    sources: dict[str, int] = {}
    for offer in offers:
        sources[offer.source] = sources.get(offer.source, 0) + 1
    return {
        "ok": True,
        "checkedAt": datetime.now(UTC).isoformat(),
        "total": len(offers),
        "sources": sources,
        "readOnly": True,
    }


@server.tool(
    name="search_climate_products",
    title="Найти климатическую технику",
    description=(
        "Ищет модели, закупочные цены, остатки и характеристики в собственном климатическом "
        "каталоге Русклимата, Daichi, Бриза и JAC. Не создаёт заказов и резервов."
    ),
    annotations=READ_ONLY,
)
def search_climate_products(query: str, limit: int = 20, force_refresh: bool = False) -> dict[str, object]:
    return build_search_response(load_live_offers(force=force_refresh), query, limit=limit)


def _product_payload(offer: Offer, checked_at: str) -> dict[str, object]:
    source = offer.source.casefold()
    supplier = SOURCE_LABELS.get(source, offer.source)
    attributes = [
        {"label": "Источник", "key": "source", "value": source},
        {"label": "Количество", "key": "stock_quantity", "value": offer.stock, "numericValue": offer.stock},
        {"label": "BTU", "key": "btu", "value": offer.btu_calc},
    ]
    attributes.extend(
        {"label": str(key), "key": str(key), "value": value}
        for key, value in offer.attrs.items()
        if value not in (None, "")
    )
    part = offer.supplier_sku.split(":", 1)[-1]
    return {
        "source": source,
        "supplierName": supplier,
        "sku": offer.supplier_sku,
        "name": " ".join(part for part in (offer.brand, offer.model) if part),
        "purchasePriceGross": float(offer.cost) if offer.cost is not None else None,
        "stockStatus": "available" if offer.stock > 0 else "out",
        "isAvailable": offer.stock > 0,
        "stockQuantity": offer.stock,
        "warehouse": "Симферополь",
        "deliveryDays": None,
        "vendor": offer.brand,
        "part": part,
        "category": "климатическая техника",
        "description": f"{supplier}; остаток {offer.stock}; BTU {offer.btu_calc or 'не указан'}",
        "productUrl": offer.photos[0] if offer.photos else "",
        "updatedAt": checked_at,
        "attributes": attributes,
        "specifications": {"btu": offer.btu_calc, "series": offer.series, **offer.attrs},
    }


def _normalize(value: str) -> str:
    return " ".join(_TOKEN_RE.findall(value.casefold().replace("ё", "е")))


def _tokens(value: str) -> list[str]:
    return [token for token in value.split() if len(token) >= 2]


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
