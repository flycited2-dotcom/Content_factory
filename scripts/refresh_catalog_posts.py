"""Перепроверяет ещё не вышедшие каталожные посты по текущим правилам отбора.

Правила отбора ужесточились (порог насыщенности, группа по названию, лучшее из
нескольких фото), а посты, поставленные в план раньше, остались со старым текстом
и старой картинкой. Скрипт для каждого поста в статусах planned/review ищет позицию
в снимке каталога:
  — позиции нет или она больше не проходит порог — пост снимается, слот займёт другой;
  — иначе пересобираются текст, категория и фото; если что-то изменилось, пост
    возвращается на ревью, чтобы владелец увидел именно то, что выйдет в ленту.
Одобренные и уже отправленные в VK не трогаются. Идемпотентен.

    python scripts/refresh_catalog_posts.py [--dry-run]
"""
import re
import sys
from pathlib import Path

import httpx

ROOT = Path("/opt/content-factory-vk")
sys.path.insert(0, str(ROOT / "src"))

from content_factory.orchestrator.vk_catalog_plan import (  # noqa: E402
    CATALOG_PREFIX, GROUP_CATEGORY, build_caption)
from content_factory.orchestrator.vk_content_plan import VkContentPlanStore  # noqa: E402
from content_factory.storefront.catalog_snapshot import load_snapshot  # noqa: E402
from content_factory.storefront.product_posts import is_postable, prepare_photo  # noqa: E402

PRICE = re.compile(r"💎 ([0-9][0-9 ]*) ₽")
dry_run = "--dry-run" in sys.argv

store = VkContentPlanStore(str(ROOT / "state" / "vk-plan.db"))
catalog = {item.id: item for item in load_snapshot(ROOT / "state" / "catalog-snapshot.json")}
dropped = rewritten = repictured = kept = 0
with httpx.Client(timeout=40) as client:
    for plan_item in store.list():
        if not plan_item.source_key.startswith(CATALOG_PREFIX):
            continue
        if plan_item.status not in {"planned", "review"}:
            continue
        item = catalog.get(plan_item.source_key[len(CATALOG_PREFIX):])
        if item is None or not is_postable(item):
            reason = "нет в снимке" if item is None else "слишком бедное описание"
            print(f"{plan_item.id}: снят ({reason}) — {plan_item.caption.splitlines()[2][:60]}")
            if not dry_run:
                store._transition(plan_item.id, ("planned", "review"), "superseded")
            dropped += 1
            continue

        changed = []
        price_match = PRICE.search(plan_item.caption)
        price = int(price_match.group(1).replace(" ", "")) if price_match else item.price
        fresh = build_caption(item, price)
        if fresh != plan_item.caption:
            changed.append("текст")
        category = GROUP_CATEGORY[item.group]
        if category != plan_item.category:
            changed.append(f"категория {plan_item.category}→{category}")

        # Фото: готовим во временный файл и сравниваем с тем, что лежит в плане.
        image = Path(plan_item.card_path)
        candidate = image.with_name(image.stem + ".new.jpg")
        picture_changed = False
        if prepare_photo(client, [item.picture, *item.pictures], candidate) is not None:
            if not image.is_file() or candidate.read_bytes() != image.read_bytes():
                picture_changed = True
                changed.append("фото")
                if not dry_run:
                    candidate.replace(image)
            else:
                candidate.unlink()
        if not changed:
            kept += 1
            continue
        print(f"{plan_item.id}: обновлено ({', '.join(changed)}) — {item.name[:55]}")
        if dry_run:
            if candidate.exists():
                candidate.unlink()
            continue
        with store._connect() as connection:
            connection.execute(
                "UPDATE vk_content_plan SET caption=?,category=?,"
                "status=CASE WHEN status='review' THEN 'planned' ELSE status END,"
                "telegram_message_id=CASE WHEN status='review' THEN NULL ELSE telegram_message_id END "
                "WHERE id=? AND status IN ('planned','review')",
                (fresh, category, plan_item.id))
        rewritten += int(fresh != plan_item.caption)
        repictured += int(picture_changed)
print(f"итого: снято {dropped}, текст/категория обновлены {rewritten}, фото заменено {repictured}, "
      f"без изменений {kept}" + (" (пробный запуск, ничего не записано)" if dry_run else ""))
