"""Живой манифест опорных товаров для VK-магазина.

Модуль не пытается обойти ограничения VK: он готовит и ежедневно перепроверяет только
те позиции, у которых есть наличие, цена, квадратная карточка и рабочая ссылка заявки.
Нативная выгрузка в VK включается отдельным этапом после получения прав на фотографии.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image
from decouple import config

from content_factory.orchestrator.vk_content_plan import (
    VkPlanCandidate,
    classify_category,
    load_candidates,
)
from content_factory.publish.orders import OrderLinks
from content_factory.publish.vk_text_sync import build_live_caption_map
from content_factory.dedupe import canonical_product_identity


PRICE_RE = re.compile(r"(?:от\s+)?([\d\s]+)\s*₽")
CATEGORY_NAMES = {
    "air_conditioners": "Кондиционеры",
    "heat_pumps": "Тепловые насосы",
    "stabilizers": "Стабилизаторы напряжения",
    "ups": "Источники бесперебойного питания",
    "recuperators": "Рекуператоры",
    "ventilation": "Вентиляция",
    "climate": "Климатическая техника",
}


@dataclass(frozen=True)
class VkStorefrontItem:
    item_id: str
    source_key: str
    title: str
    price: int
    category: str
    collection: str
    description: str
    image_path: str
    image_url: str
    image_width: int
    image_height: int
    order_url: str
    stock_status: str = "available"


def _price(caption: str) -> int | None:
    match = PRICE_RE.search(caption or "")
    return int(re.sub(r"\s", "", match.group(1))) if match else None


def _image_size(path: str) -> tuple[int, int] | None:
    try:
        with Image.open(path) as image:
            return tuple(map(int, image.size))
    except (OSError, ValueError):
        return None


def select_anchor_candidates(candidates: list[VkPlanCandidate], limit: int = 20) -> list[VkPlanCandidate]:
    """Ротация брендов/категорий без массовой выгрузки всего каталога."""
    ordered = sorted(candidates, key=lambda item: (-item.source_ts, item.source_key))
    selected = []
    while ordered and len(selected) < int(limit):
        previous = selected[-1] if selected else None
        index = next((i for i, item in enumerate(ordered)
                      if previous is None or (item.brand != previous.brand
                                              and item.category != previous.category)), None)
        if index is None:
            index = next((i for i, item in enumerate(ordered)
                          if previous is None or item.brand != previous.brand), 0)
        selected.append(ordered.pop(index))
    return selected


def build_items(candidates: list[VkPlanCandidate], *, limit: int, public_image_base: str,
                order_bot: str, order_links: OrderLinks) -> tuple[list[VkStorefrontItem], list[str]]:
    valid = []
    rejected = []
    for candidate in candidates:
        size = _image_size(candidate.card_path)
        price = _price(candidate.caption)
        if not size:
            rejected.append(f"{candidate.source_key}: изображение не читается")
            continue
        if size[0] != size[1]:
            rejected.append(f"{candidate.source_key}: первая карточка не квадратная {size[0]}x{size[1]}")
            continue
        if not price or price <= 0:
            rejected.append(f"{candidate.source_key}: нет актуальной цены")
            continue
        valid.append(candidate)

    # Один и тот же аппарат может прийти от нескольких поставщиков под разными
    # source_key. Оставляем наиболее свежую запись канонической модели.
    unique: list[VkPlanCandidate] = []
    seen_models: dict[str, str] = {}
    for candidate in sorted(valid, key=lambda item: (-item.source_ts, item.source_key)):
        identity = canonical_product_identity(
            source_key=candidate.source_key, caption=candidate.caption,
            category=candidate.category, brand=candidate.brand,
        )
        if identity in seen_models:
            rejected.append(
                f"{candidate.source_key}: дубль модели {seen_models[identity]}"
            )
            continue
        seen_models[identity] = candidate.source_key
        unique.append(candidate)

    items = []
    for candidate in select_anchor_candidates(unique, limit):
        size = _image_size(candidate.card_path)
        price = _price(candidate.caption)
        title = next(line.strip() for line in candidate.caption.splitlines() if line.strip())
        code = order_links.code_for(candidate.source_key)
        filename = Path(candidate.card_path).name
        identity = canonical_product_identity(
            source_key=candidate.source_key, caption=candidate.caption,
            category=candidate.category, brand=candidate.brand,
        )
        items.append(VkStorefrontItem(
            item_id=hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12],
            source_key=candidate.source_key, title=title[:120], price=int(price),
            category=candidate.category,
            collection=CATEGORY_NAMES.get(candidate.category, CATEGORY_NAMES["climate"]),
            description=candidate.caption, image_path=candidate.card_path,
            image_url=f"{public_image_base.rstrip('/')}/{filename}",
            image_width=size[0], image_height=size[1],
            order_url=f"https://t.me/{order_bot.lstrip('@')}?start=ord_{code}",
        ))
    return items, rejected


class VkStorefrontStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS vk_storefront_items ("
                "item_id TEXT PRIMARY KEY,source_key TEXT NOT NULL,title TEXT NOT NULL,"
                "price INTEGER NOT NULL,category TEXT NOT NULL,payload_json TEXT NOT NULL,"
                "status TEXT NOT NULL,updated_at INTEGER NOT NULL)"
            )

    def sync(self, items: list[VkStorefrontItem]) -> dict[str, int]:
        now = int(time.time())
        active_ids = {item.item_id for item in items}
        added = changed = 0
        with sqlite3.connect(self.path) as connection:
            previous = {row[0]: (row[1], row[2]) for row in connection.execute(
                "SELECT item_id,price,payload_json FROM vk_storefront_items WHERE status='active'"
            )}
            for item in items:
                payload = json.dumps(asdict(item), ensure_ascii=False, sort_keys=True)
                if item.item_id not in previous:
                    added += 1
                elif previous[item.item_id] != (item.price, payload):
                    changed += 1
                connection.execute(
                    "INSERT INTO vk_storefront_items "
                    "(item_id,source_key,title,price,category,payload_json,status,updated_at) "
                    "VALUES(?,?,?,?,?,?,'active',?) ON CONFLICT(item_id) DO UPDATE SET "
                    "source_key=excluded.source_key,title=excluded.title,price=excluded.price,"
                    "category=excluded.category,payload_json=excluded.payload_json,"
                    "status='active',updated_at=excluded.updated_at",
                    (item.item_id, item.source_key, item.title, item.price, item.category,
                     payload, now),
                )
            if active_ids:
                placeholders = ",".join("?" for _ in active_ids)
                removed = connection.execute(
                    f"UPDATE vk_storefront_items SET status='inactive',updated_at=? "
                    f"WHERE status='active' AND item_id NOT IN ({placeholders})",
                    (now, *active_ids),
                ).rowcount
            else:
                removed = connection.execute(
                    "UPDATE vk_storefront_items SET status='inactive',updated_at=? "
                    "WHERE status='active'", (now,),
                ).rowcount
        return {"added": added, "changed": changed, "removed": int(removed)}


def collect_candidates(source_db: str, configs: list[str]) -> tuple[list[VkPlanCandidate], list[str]]:
    merged = {}
    errors = []
    for config_path in configs:
        try:
            merged.update(build_live_caption_map(config_path))
        except Exception as exc:
            errors.append(f"{config_path}: {exc}")
    return load_candidates(source_db, merged), errors


def write_manifest(path: str | Path, items: list[VkStorefrontItem], rejected: list[str]) -> None:
    payload = {
        "generated_at": int(time.time()),
        "items": [asdict(item) for item in items],
        "collections": sorted({item.collection for item in items}),
        "collection_structure": list(dict.fromkeys(CATEGORY_NAMES.values())),
        "collection_counts": {
            name: sum(item.collection == name for item in items)
            for name in dict.fromkeys(CATEGORY_NAMES.values())
        },
        "rejected": rejected,
        "native_vk_upload": False,
        "blocking_reason": "VK token cannot upload community product photos",
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VK storefront anchor manifest")
    parser.add_argument("--source-db", default=os.getenv(
        "CF_SOURCE_DB", "/opt/content-factory/state/content_factory.db"))
    parser.add_argument("--state-db", default=os.getenv(
        "VK_PLAN_STATE_DB", "/opt/content-factory-vk/state/vk-plan.db"))
    parser.add_argument("--output", default=os.getenv(
        "VK_STOREFRONT_MANIFEST", "/opt/content-factory-vk/state/vk-storefront.json"))
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--apply", action="store_true", help="сохранить живой манифест и состояние")
    args = parser.parse_args(argv)
    configs = [
        os.getenv("CONTENT_FACTORY_CONFIG", "/opt/content-factory/config/config.yaml"),
        os.getenv("STABILIZER_CONFIG", "/opt/content-factory-vk/config/stabilizers-b2c.yaml"),
    ]
    candidates, errors = collect_candidates(args.source_db, configs)
    links = OrderLinks(args.source_db)
    items, rejected = build_items(
        candidates, limit=max(15, min(args.limit, 30)),
        public_image_base=config("CF_PUBLIC_IMAGE_BASE", "https://splithome.ru/static/cf-cards"),
        order_bot=config("TELEGRAM_ORDER_BOT", "Sendpr1ce_bot"), order_links=links,
    )
    result = {"items": len(items), "collections": sorted({item.collection for item in items}),
              "rejected": len(rejected), "source_errors": errors, "applied": False}
    if args.apply:
        result.update(VkStorefrontStore(args.state_db).sync(items))
        write_manifest(args.output, items, rejected)
        result["applied"] = True
    print(json.dumps(result, ensure_ascii=False))
    return 1 if len(items) < 15 else 0


if __name__ == "__main__":
    raise SystemExit(main())
