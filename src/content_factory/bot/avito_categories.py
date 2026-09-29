"""Small, stable Telegram keyboards for finite Avito category batches."""
from __future__ import annotations

import hashlib
import re

from content_factory.ready_price import category_counts, control_command

PAGE_SIZE = 8


def category_key(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]


def find_category(categories: list[tuple[str, int]], key: str) -> tuple[str, int] | None:
    return next(((name, count) for name, count in categories
                 if category_key(name) == key), None)


def category_from_text(categories: list[tuple[str, int]], text: str) -> tuple[str, int] | None:
    """Accept a category name copied from the count list, including ' — 7'."""
    query = re.sub(r"\s+—\s+\d+\s*$", "", text.strip()).casefold()
    return next(((name, count) for name, count in categories
                 if name.casefold() == query), None)


def category_pages(categories: list[tuple[str, int]], page: int = 0) -> tuple[str, dict]:
    pages = max(1, (len(categories) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    shown = categories[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    rows = [[{"text": f"{name} · {count}", "callback_data": f"avito:cat:{category_key(name)}"}]
            for name, count in shown]
    nav = []
    if page:
        nav.append({"text": "◀️ Назад", "callback_data": f"avito:cats:{page - 1}"})
    if page + 1 < pages:
        nav.append({"text": "Далее ▶️", "callback_data": f"avito:cats:{page + 1}"})
    if nav:
        rows.append(nav)
    rows.append([{"text": "📊 Статус Avito", "callback_data": "avito:status"}])
    return (f"📦 Выберите категорию ({page + 1}/{pages}). "
            "После выбора укажите размер партии:", {"inline_keyboard": rows})


def batch_choices(name: str, available: int) -> tuple[str, dict]:
    options = sorted({min(size, available) for size in (1, 5, 10, 20) if available > 0})
    rows = [[{"text": f"▶️ Запустить {size}",
              "callback_data": f"avito:go:{category_key(name)}:{size}"}]
            for size in options]
    rows.append([{"text": "◀️ Другие категории", "callback_data": "avito:categories"}])
    return (f"📦 {name}: доступно {available}. Выберите, сколько товаров "
            "поставить в генерацию:", {"inline_keyboard": rows})


def category_action(action: str, state_db, catalog_db) -> tuple[str, dict | None] | None:
    """Handle only category navigation and explicit finite-batch callbacks."""
    parts = action.split(":")
    if parts[0] not in {"categories", "cats", "cat", "go"}:
        return None
    try:
        categories = category_counts(state_db, catalog_db)
    except ValueError as exc:
        return f"❌ {exc}", None
    if parts[0] == "categories":
        return category_pages(categories)
    if parts[0] == "cats" and len(parts) == 2 and parts[1].isdigit():
        return category_pages(categories, int(parts[1]))
    if parts[0] == "cat" and len(parts) == 2:
        found = find_category(categories, parts[1])
        return batch_choices(*found) if found else ("Категория изменилась. Откройте список заново.", None)
    if parts[0] == "go" and len(parts) == 3 and parts[2].isdigit():
        found = find_category(categories, parts[1])
        if not found:
            return "Категория изменилась. Откройте список заново.", None
        return control_command(f"start {parts[2]} ={found[0]}", state_db, catalog_db), None
    return "Кнопка устарела. Откройте «Категории» заново.", None


def category_text_action(arg: str, state_db, catalog_db) -> tuple[str, dict] | None:
    """Turn /avito categories or a copied category line into visible buttons."""
    raw = arg.strip()
    if raw.casefold() not in {"categories", "категории"} and not re.search(r"\s+—\s+\d+\s*$", raw):
        return None
    categories = category_counts(state_db, catalog_db)
    if raw.casefold() in {"categories", "категории"}:
        return category_pages(categories)
    found = category_from_text(categories, raw)
    return batch_choices(*found) if found else category_pages(categories)
