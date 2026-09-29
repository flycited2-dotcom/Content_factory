from content_factory.bot import avito_categories as menu
from content_factory.bot.avito_categories import (
    batch_choices, category_from_text, category_key, category_pages, find_category,
)


def test_category_buttons_select_exact_name_and_batch_size():
    categories = [(f"Группа {i}", 7) for i in range(10)]
    title, markup = category_pages(categories)
    assert "1/2" in title
    assert len(markup["inline_keyboard"]) == 10
    key = category_key("Группа 0")
    assert markup["inline_keyboard"][0][0]["callback_data"] == f"avito:cat:{key}"
    assert find_category(categories, key) == ("Группа 0", 7)
    assert category_from_text(categories, "Группа 0 — 7") == ("Группа 0", 7)
    title, choice = batch_choices("Группа 0", 7)
    assert "доступно 7" in title
    assert [row[0]["callback_data"] for row in choice["inline_keyboard"][:-1]] == [
        f"avito:go:{key}:1", f"avito:go:{key}:5", f"avito:go:{key}:7"]


def test_category_page_is_bounded():
    categories = [(f"Группа {i}", 1) for i in range(10)]
    title, markup = category_pages(categories, 500)
    assert "2/2" in title
    assert markup["inline_keyboard"][0][0]["text"] == "Группа 8 · 1"


def test_category_callback_waits_for_explicit_size_before_start(monkeypatch):
    categories = [("Сушилки для овощей и фруктов", 7)]
    calls = []
    monkeypatch.setattr(menu, "category_counts", lambda *_: categories)
    monkeypatch.setattr(menu, "control_command", lambda *args: calls.append(args[0]) or "Старт")
    name = categories[0][0]
    key = category_key(name)
    assert menu.category_action("categories", "state", "catalog")[1]["inline_keyboard"]
    assert menu.category_action(f"cat:{key}", "state", "catalog")[0].startswith("📦")
    assert menu.category_text_action(f"{name} — 7", "state", "catalog")[0].startswith("📦")
    assert calls == []
    assert menu.category_action(f"go:{key}:5", "state", "catalog")[0] == "Старт"
    assert calls == [f"start 5 ={name}"]
