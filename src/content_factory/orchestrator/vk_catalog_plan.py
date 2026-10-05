"""Календарь товарных постов VK из каталога в наличии.

Слоты чередуются: товар — экспертный пост — товар… Для товарного слота группа
(кондиционеры, обогрев, вентиляция…) берётся из сезонных весов, затем выбирается
конкретная позиция, чья страница на сайте открывается и показывает «в наличии».
Все данные в посте — цена, наличие, ссылка — проверены на живой странице.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx

from content_factory.orchestrator.vk_content_plan import (
    ACTIVE_STATUSES,
    VkContentPlanStore,
    VkPlanCandidate,
    plan_slots,
    slot_kind,
    slot_ordinal,
)
from content_factory.storefront.product_posts import (
    MONTH_WEIGHTS,
    PRICE_CAP,
    CatalogItem,
    group_for_slot,
    live_check,
    money,
    pick_item,
    prepare_photo,
    write_post,
)

CATALOG_PREFIX = "catalog:"
GROUP_CATEGORY = {
    "ac": "air_conditioners", "vent": "ventilation", "air": "air_care",
    "heater": "heaters", "radiator": "heating", "water": "water_heaters",
    "floor": "floor_heating",
}
UTM = "utm_source=vk&utm_medium=organic_social&utm_campaign=catalog_post&utm_content={cid}"
REPEAT_DAYS = 120
ATTEMPTS_PER_SLOT = 12
_LINK = re.compile(r"https://splithome\.ru/product/[^\s]+")
_PRICE_LINE = re.compile(r"(💎 )([0-9][0-9 ]*)( ₽)")


def link_line(item: CatalogItem) -> str:
    cid = quote(item.id, safe="")
    separator = "&" if "?" in item.url else "?"
    return f"🛒 Смотреть и заказать: {item.url}{separator}{UTM.format(cid=cid)}"


def build_caption(item: CatalogItem, price: int) -> str:
    return f"{write_post(item, price)}\n\n{link_line(item)}"


def load_catalog_items(catalog_dir, snapshot_path, refresh=None) -> tuple[list[CatalogItem], dict]:
    """Позиции для ленты: свежий снимок из базы сайта, а при неудаче — прежний или старая выгрузка.

    Сбой обновления не должен ни ронять цикл планировщика, ни считаться его ошибкой:
    три «ошибочных» цикла подряд переводят контент-завод в L0 и останавливают посты.
    """
    from content_factory.storefront.catalog_snapshot import load_snapshot, refresh_snapshot
    from content_factory.storefront.product_posts import load_catalog

    report: dict = {}
    items: list[CatalogItem] = []
    if snapshot_path:
        try:
            report = (refresh or refresh_snapshot)(snapshot_path)
            if Path(snapshot_path).is_file():
                items = load_snapshot(snapshot_path)
                report["source"] = "site"
        except Exception as error:  # noqa: BLE001 — любая причина = откат на выгрузку
            report = {"status": f"failed: {type(error).__name__}", "source": "yml"}
    if not items:
        items = load_catalog(catalog_dir)
        report["source"] = "yml"
    return items, report


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)


def materialize_catalog_plan(store: VkContentPlanStore, items: list[CatalogItem],
                             now: datetime, client: httpx.Client, photo_dir: str | Path,
                             horizon_days: int = 14, fill_editorial_gaps: bool = True,
                             fetch=live_check, photo=prepare_photo) -> list[int]:
    """Заполнить свободные товарные слоты; незаполненные экспертные — по желанию."""
    plan = store.list()
    occupied = {item.due_at for item in plan if item.status in ACTIVE_STATUSES}
    cutoff = int((now - timedelta(days=REPEAT_DAYS)).timestamp())
    taken = {item.source_key[len(CATALOG_PREFIX):] for item in plan
             if item.source_key.startswith(CATALOG_PREFIX) and item.due_at >= cutoff}
    recent = [item.brand.casefold() for item in sorted(plan, key=lambda x: x.due_at)
              if item.content_type == "product"][-2:]
    skipped: set[str] = set()
    added: list[int] = []
    photo_dir = Path(photo_dir)

    for due_at in plan_slots(now, horizon_days=horizon_days):
        if due_at in occupied:
            continue
        if slot_kind(due_at) != "product" and not fill_editorial_gaps:
            continue
        ordinal = slot_ordinal(due_at)
        day = datetime.fromtimestamp(due_at).date()
        group = group_for_slot(ordinal, day)
        # Запасные группы по убыванию сезонного веса: слот не должен пустовать только
        # потому, что подходящие товары одной группы закончились.
        weights = MONTH_WEIGHTS[day.month]
        fallbacks = [g for g in sorted(weights, key=lambda g: -weights[g]) if g != group]
        for _ in range(ATTEMPTS_PER_SLOT):
            item = pick_item(items, group, taken | skipped, recent, salt=str(ordinal))
            if item is None and fallbacks:
                group = fallbacks.pop(0)
                continue
            if item is None:
                break
            live = fetch(client, item)
            if live is None or not live.in_stock or not 0 < live.price <= PRICE_CAP[item.group]:
                skipped.add(item.id)
                continue
            image = photo_dir / f"{_safe(item.id)}.jpg"
            if not image.is_file() and photo(client, [item.picture, *item.pictures], image) is None:
                skipped.add(item.id)
                continue
            candidate = VkPlanCandidate(
                source_key=f"{CATALOG_PREFIX}{item.id}", source_ts=float(due_at),
                caption=build_caption(item, live.price), card_path=str(image.resolve()),
                category=GROUP_CATEGORY[item.group], brand=item.brand.upper(),
                content_type="product",
            )
            item_id = store.add(candidate, due_at)
            taken.add(item.id)
            if item_id is None:  # дубль по отпечатку текста — берём следующую позицию
                continue
            added.append(item_id)
            recent = (recent + [item.brand.casefold()])[-2:]
            occupied.add(due_at)
            break
    return added


def refresh_catalog_items(store: VkContentPlanStore, client: httpx.Client, now: datetime,
                          hours: int = 72, fetch_url=None) -> dict[str, list[int]]:
    """Перед публикацией ещё раз сверить ближайшие посты с живой страницей.

    Нет в наличии — снимаем; изменилась цена — обновляем текст и возвращаем на ревью,
    чтобы владелец не одобрял одну цену, а на стену уходила другая.
    """
    from content_factory.storefront.product_posts import parse_live

    def default_fetch(url: str):
        try:
            response = client.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
        except httpx.HTTPError:
            return None
        return parse_live(response.text) if response.status_code == 200 else None

    fetch_url = fetch_url or default_fetch
    result = {"blocked": [], "repriced": []}
    upper = int((now + timedelta(hours=hours)).timestamp())
    for item in store.list():
        if not (item.source_key.startswith(CATALOG_PREFIX)
                and item.status in {"planned", "review", "approved"}
                and int(now.timestamp()) < item.due_at <= upper):
            continue
        link = _LINK.search(item.caption)
        live = fetch_url(link.group(0).split("?")[0]) if link else None
        if live is None:  # сеть или разметка подвели — не решаем за сайт
            continue
        if not live.in_stock:
            if store._transition(item.id, ("planned", "review", "approved"), "blocked_unavailable"):
                result["blocked"].append(item.id)
            continue
        current = _PRICE_LINE.search(item.caption)
        if current and int(current.group(2).replace(" ", "")) != live.price:
            fresh = _PRICE_LINE.sub(lambda m: f"{m.group(1)}{money(live.price)[:-2]}{m.group(3)}",
                                    item.caption, count=1)
            if store.update_caption(item.id, fresh):
                result["repriced"].append(item.id)
    return result


SOLD_PREFIX = "⛔ Товар закончился. Актуальные позиции в наличии — на сайте: https://splithome.ru/catalog/"
PUBLISHED_STATUSES = ("photo_pending", "photo_confirmed", "photo_overdue",
                      "published_unverified", "published")
MARK_EVENTS = ("marked_sold", "marked_available")
RECONCILE_DAYS = 45
RECONCILE_LIMIT = 10


def reconcile_published(store: VkContentPlanStore, publisher, now: datetime, compose,
                        fetch_url=None, client=None, days: int = RECONCILE_DAYS) -> dict[str, list[int]]:
    """Опубликованный пост не должен обещать товар, которого уже нет.

    Человек долистал до записи недельной давности и хочет заказать, а остатка нет.
    Если на странице товара «нет в наличии», первой строкой поста ставится пометка;
    вернулся в наличие — пометка снимается. Правится только текст: фото остаётся.
    Отложенная запись сохраняет дату публикации, иначе VK выпустит её сразу.
    """
    from content_factory.storefront.product_posts import parse_live

    def default_fetch(url: str):
        try:
            response = client.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
        except httpx.HTTPError:
            return None
        return parse_live(response.text) if response.status_code == 200 else None

    fetch_url = fetch_url or default_fetch
    result: dict[str, list[int]] = {"marked": [], "restored": [], "failed": []}
    since = int((now - timedelta(days=days)).timestamp())
    checked = 0
    for item in store.list():
        if not (item.source_key.startswith(CATALOG_PREFIX) and item.status in PUBLISHED_STATUSES
                and item.vk_post_id and item.due_at >= since):
            continue
        if checked >= RECONCILE_LIMIT:
            break
        link = _LINK.search(item.caption)
        live = fetch_url(link.group(0).split("?")[0]) if link else None
        checked += 1
        if live is None:  # сеть или разметка подвели — не решаем за сайт
            continue
        marked = store.last_event(item.id, MARK_EVENTS) == "marked_sold"
        if live.in_stock == (not marked):
            continue  # пост уже в правильном состоянии
        body = compose(item)
        text = body if live.in_stock else f"{SOLD_PREFIX}\n\n{body}"
        postponed = item.due_at > int(now.timestamp())
        reply = publisher.edit_text(int(item.vk_post_id), text,
                                    publish_at=item.due_at if postponed else None)
        if not reply.ok:
            result["failed"].append(item.id)
            break  # права или VK недоступны — остальные записи тоже не пройдут
        if getattr(reply, "dry_run", False):
            continue
        store.record_event(item.id, "marked_available" if live.in_stock else "marked_sold", text[:120])
        result["restored" if live.in_stock else "marked"].append(item.id)
    return result
