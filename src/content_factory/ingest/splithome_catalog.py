"""Источник «SplitHome»: весь ассортимент сайта splithome.ru в наличии в Симферополе (БД сайта oasis).

Владелец, 2026-10-21: завод берёт весь ассортимент splithome.ru, но публикует НЕ РРЦ сайта, а нашу
оптовую цену — дилерский вход +10% с округлением вверх до …90 (так считает splithub.ru: Бриз 47 400 →
52 190). Вход по поставщику (как opt_resolver): Бриз — из API Бриза (в БД у Бриза лежит розница),
Русклимат — складская цена, Daichi/JAC — «Ваша цена» из БД. Входа нет — позицию не берём.
Кондиционеры, которые уже приходят со splithub.ru (тот же код модели), не дублируем.

Снапшот prices_dir/splithome.json обновляет splithub_sync (тот же суточный таймер).
"""
from __future__ import annotations

import json
import math
from decimal import Decimal
from pathlib import Path

from content_factory.ingest.excel_price import PriceItem

SLOT = "splithome"
SNAPSHOT_NAME = "splithome.json"
MARKUP = Decimal("1.10")

QUERY = """
SELECT p.source, p.nc_code, p.title, b.title AS brand, c.title AS category,
       p.price_wholesale, s.price_base, s.quantity
FROM catalog_product p
JOIN stock_stock s ON s.product_id = p.id
LEFT JOIN catalog_brand b ON b.id = p.brand_id
LEFT JOIN catalog_category c ON c.id = p.category_id
WHERE p.is_active = TRUE AND s.warehouse = %(warehouse)s AND s.quantity > 0
ORDER BY c.title, b.title NULLS LAST, p.title
"""


def round_up_90(value: float) -> int:
    """Вверх до …90: 52 140 → 52 190, 52 190 → 52 190."""
    return int(math.ceil((value - 90) / 100.0) * 100 + 90)


def dealer_cost(row: dict, breez_base: dict) -> Decimal | None:
    source = (row.get("source") or "").lower()
    if source == "breeze":
        base = breez_base.get(str(row.get("nc_code")))
        return Decimal(str(base)) if base is not None else None      # розницу Бриза не публикуем
    if source == "rusklimat":
        value = row.get("price_base") or row.get("price_wholesale")
    else:
        value = row.get("price_wholesale")
    return Decimal(str(value)) if value else None


def product_name(row: dict) -> str:
    title, brand = (row.get("title") or "").strip(), (row.get("brand") or "").strip()
    return title if not brand or brand.lower() in title.lower() else f"{brand} {title}"


def build_snapshot(rows: list[dict], breez_base: dict, skip_codes: set[str]) -> list[dict]:
    from content_factory.ingest.site_specs import model_codes
    out, seen = [], set()
    for row in rows:
        cost = dealer_cost(row, breez_base)
        if not cost or cost <= 0:
            continue
        name = product_name(row)
        codes = set(model_codes(name))
        if codes & skip_codes:
            continue                                   # уже в слоте splithub.ru
        nc = str(row.get("nc_code") or "")
        if not nc or nc in seen:
            continue
        seen.add(nc)
        out.append({"sku": nc, "brand": (row.get("brand") or "").strip(), "name": name,
                    "section": (row.get("category") or "Прочее").strip(), "source": row.get("source"),
                    "cost": float(cost), "price": round_up_90(float(cost * MARKUP))})
    return out


def snapshot_path(prices_dir) -> Path:
    return Path(prices_dir) / SNAPSHOT_NAME


def write_snapshot(prices_dir, items: list[dict]) -> None:
    path = snapshot_path(prices_dir)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def read_snapshot(prices_dir) -> list[dict]:
    path = snapshot_path(prices_dir)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("items", [])


def load_items(prices_dir) -> list[PriceItem]:
    return [PriceItem(section=e["section"], article=e["sku"], brand=e["brand"], name=e["name"],
                      price=int(e["price"])) for e in read_snapshot(prices_dir)]


def fetch_rows(dsn: dict, warehouse: str) -> list[dict]:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    conn = psycopg2.connect(connect_timeout=10, **dsn)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(QUERY, {"warehouse": warehouse})
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def sync(prices_dir, dsn: dict, warehouse: str, breez_base: dict) -> dict:
    """Суточное обновление снапшота; коды моделей splithub.ru — чтобы не дублировать."""
    from content_factory.ingest import splithub_catalog
    from content_factory.ingest.site_specs import model_codes
    skip = {c for e in splithub_catalog.read_snapshot(prices_dir) for c in model_codes(e.get("name", ""))}
    rows = fetch_rows(dsn, warehouse)
    if not rows:
        raise RuntimeError("splithome: БД сайта вернула пусто — снапшот не трогаем")
    snap = build_snapshot(rows, breez_base, skip)
    write_snapshot(prices_dir, snap)
    by_section: dict[str, int] = {}
    for e in snap:
        by_section[e["section"]] = by_section.get(e["section"], 0) + 1
    return {"in_stock": len(rows), "listed": len(snap), "sections": by_section}
