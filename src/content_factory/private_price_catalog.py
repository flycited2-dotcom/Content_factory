"""Read-only tender search over private supplier price files.

The mail/Telegram ingestion jobs own synchronization.  This module never logs in
to mail and never changes a price file; it only builds a searchable view over the
current supplier slots in ``state/prices``.
"""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
import json
from pathlib import Path
import re
from typing import Iterable

from content_factory.ingest.excel_price import PriceItem, load_price_slots, stem


MAX_LIMIT = 100
_TOKEN_RE = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)
_GENERIC_TOKENS = {
    "для", "или", "поставка", "поставить", "продажа", "купить", "товар",
    "товары", "оборудование", "изделие", "изделия", "комплект", "комплекты",
    "шт", "штук", "техническое", "задание", "требование", "требования",
}


def prices_dir_from_config() -> Path:
    from content_factory.config import load_config
    from decouple import config

    cfg = load_config(Path(config("CONTENT_FACTORY_CONFIG", "config/config.yaml")))
    return Path(cfg.state.db).parent / "prices"


def search_private_price_catalog(
    prices_dir: Path,
    query: str,
    *,
    limit: int = 20,
) -> dict[str, object]:
    normalized = _normalize(query)
    if not normalized:
        raise ValueError("Поисковый запрос не может быть пустым")
    query_tokens = [stem(token) for token in _tokens(normalized) if token not in _GENERIC_TOKENS]
    if not query_tokens:
        query_tokens = [stem(token) for token in _tokens(normalized)]

    metadata = _load_source_metadata(prices_dir)
    ranked: list[tuple[float, int, str, str, PriceItem]] = []
    for slot, items in load_price_slots(prices_dir):
        for item in items:
            name = _normalize(item.name)
            section = _normalize(item.section)
            brand = _normalize(item.brand)
            article = _normalize(item.article)
            name_haystack = " ".join((brand, name, article)).strip()
            full_haystack = " ".join((section, name_haystack)).strip()
            name_hits = sum(token in name_haystack for token in query_tokens)
            full_hits = sum(token in full_haystack for token in query_tokens)
            if full_hits == 0:
                continue
            coverage = full_hits / max(1, len(query_tokens))
            name_coverage = name_hits / max(1, len(query_tokens))
            score = coverage * 100 + name_coverage * 80
            if normalized in name_haystack:
                score += 80
            elif normalized in full_haystack:
                score += 30
            ranked.append((-score, item.price, slot, name_haystack, item))

    ranked.sort(key=lambda row: row[:4])
    safe_limit = max(1, min(int(limit), MAX_LIMIT))
    selected = ranked[:safe_limit]
    checked_at = datetime.now(UTC).isoformat()
    products = [
        _product_payload(slot, item, checked_at, metadata.get(slot, {}), score=-score)
        for score, _, slot, _, item in selected
    ]
    return {
        "ok": True,
        "query": " ".join(query.split()),
        "total": len(ranked),
        "returned": len(products),
        "checkedAt": checked_at,
        "sources": sorted({str(product["supplierName"]) for product in products}),
        "products": products,
        "notice": (
            "Прайс является первичным источником цены, но не подтверждает текущий остаток. "
            "Перед заявкой требуется запрос поставщику."
        ),
    }


def private_price_catalog_status(prices_dir: Path) -> dict[str, object]:
    metadata = _load_source_metadata(prices_dir)
    sources = []
    total = 0
    for slot, items in load_price_slots(prices_dir):
        total += len(items)
        sections = Counter(item.section.strip() for item in items if item.section.strip())
        source_meta = metadata.get(slot, {})
        sources.append(
            {
                "slot": slot,
                "supplier": _supplier_label(slot, source_meta),
                "items": len(items),
                "capabilities": [name for name, _ in sections.most_common(20)],
                "updatedAt": source_meta.get("updated_at") or _slot_mtime(prices_dir, slot),
            }
        )
    return {
        "ok": True,
        "checkedAt": datetime.now(UTC).isoformat(),
        "total": total,
        "sources": sources,
        "readOnly": True,
    }


def _product_payload(
    slot: str,
    item: PriceItem,
    checked_at: str,
    metadata: dict[str, object],
    *,
    score: float,
) -> dict[str, object]:
    supplier = _supplier_label(slot, metadata)
    sku = item.article.strip() or f"{slot}:{_normalize(item.name)[:80]}"
    updated_at = str(metadata.get("updated_at") or checked_at)
    return {
        "source": "private_price",
        "supplierName": supplier,
        "sku": sku,
        "name": item.name,
        "purchasePriceGross": float(item.price),
        "stockStatus": "price_only_unconfirmed",
        "isAvailable": False,
        "stockQuantity": None,
        "warehouse": "",
        "deliveryDays": None,
        "vendor": item.brand,
        "part": item.article,
        "category": item.section,
        "description": (
            f"Частный прайс {supplier}; цена {item.price}; "
            "остаток и срок поставки не подтверждены"
        ),
        "productUrl": "",
        "updatedAt": updated_at,
        "attributes": [
            {"label": "Поставщик", "key": "supplier", "value": supplier},
            {"label": "Источник", "key": "source_slot", "value": slot},
            {"label": "Раздел прайса", "key": "section", "value": item.section},
            {"label": "Релевантность", "key": "route_score", "value": round(score, 2)},
        ],
        "specifications": {
            "section": item.section,
            "brand": item.brand,
            "article": item.article,
            "mail_subject": metadata.get("subject", ""),
            "original_filename": metadata.get("filename", ""),
        },
    }


def _load_source_metadata(prices_dir: Path) -> dict[str, dict[str, object]]:
    path = Path(prices_dir) / "mail_sources.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _supplier_label(slot: str, metadata: dict[str, object]) -> str:
    explicit = str(metadata.get("sender") or metadata.get("supplier") or "").strip()
    if explicit:
        return explicit
    for prefix in ("manual__", "mail__"):
        if slot.startswith(prefix):
            return slot[len(prefix):].replace("_", " ")
    return {"manual": "Ручной прайс", "channel": "Telegram-прайс", "mail": "Почтовый прайс"}.get(slot, slot)


def _slot_mtime(prices_dir: Path, slot: str) -> str:
    path = Path(prices_dir) / f"{slot}.xlsx"
    if not path.is_file():
        return ""
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()


def _normalize(value: object) -> str:
    return " ".join(_TOKEN_RE.findall(str(value or "").casefold().replace("ё", "е")))


def _tokens(value: str) -> Iterable[str]:
    return (token for token in value.split() if len(token) >= 2)
