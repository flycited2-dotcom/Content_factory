from __future__ import annotations
import json
from decimal import Decimal
from pathlib import Path
from content_factory.models import Offer, RawProduct
from content_factory.ingest.normalize import content_hash

_CAT_TEXT_TO_ID = {
    "Бытовые сплит-системы": 2,
    "Полупромышленные системы": 6,
}  # Мультисплит/Аксессуары намеренно не маппятся → отсев


def _to_decimal(v) -> Decimal | None:
    try:
        return Decimal(str(v))
    except Exception:
        return None


def load_jac_offers(path: Path) -> list[Offer]:
    if not Path(path).exists():
        return []
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    offers: list[Offer] = []
    for r in rows:
        attrs = r.get("attributes", {}) or {}
        cat_id = _CAT_TEXT_TO_ID.get((attrs.get("категория") or "").strip())
        if cat_id is None:                       # мульти/аксессуары/неизвестное — пропуск
            continue
        if int(r.get("stock_qty") or 0) <= 0:
            continue
        cost = _to_decimal(r.get("price"))
        raw = RawProduct(source="jac", nc_code=r.get("article"), brand=r.get("brand"),
                         title=r.get("name", ""), series=None, category_id=cat_id,
                         btu_calc=None, price_wholesale=cost, stock_qty=int(r["stock_qty"]),
                         image_urls=[], tech={k: str(v) for k, v in attrs.items()})
        offers.append(Offer(
            supplier_sku=f"jac:{r.get('article')}", source="jac", brand=r.get("brand") or "",
            model=r.get("name", ""), category_id=cat_id, btu_calc=None, attrs=raw.tech,
            cost=cost, retail_ref=_to_decimal(attrs.get("РРЦ")), stock=int(r["stock_qty"]),
            photos=[], series=None, content_hash=content_hash(raw),
        ))
    return offers
