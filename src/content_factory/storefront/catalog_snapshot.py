"""Свежий снимок каталога в наличии прямо из базы сайта.

Раньше список «в наличии» был разовой выгрузкой для импорта в VK Market от
31.08.2026: она нигде не обновлялась, и посты строились на устаревших остатках.
Теперь планировщик сам раз в сутки запускает короткий скрипт внутри контейнера
сайта (только чтение), получает JSON и атомарно кладёт его в state.
Любая неудача оставляет прежний снимок: лучше вчерашние данные, чем пустая лента.
"""
from __future__ import annotations

import html
import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

from content_factory.storefront.product_posts import (
    CatalogItem,
    _brand,
    group_of_item,
    parse_description,
    refine_group,
    retail_ok,
)
from content_factory.storefront.vk_catalog_export import ascii_offer_id

MARK_START = "@@CATALOG_JSON@@"
MARK_END = "@@END@@"
DEFAULT_CONTAINER = "oasis-web-1"
DUMP_SCRIPT = Path(__file__).with_name("site_dump_script.txt")
SITE = "https://splithome.ru"
MIN_ITEMS = 500  # меньше — выгрузка явно битая, прежний снимок не трогаем


def parse_dump(output: str) -> list[dict]:
    """Достать JSON между маркерами: оболочка Django печатает перед ним свои строки."""
    start = output.find(MARK_START)
    end = output.rfind(MARK_END)
    if start < 0 or end < start:
        raise ValueError("в выводе нет маркеров каталога")
    return json.loads(output[start + len(MARK_START):end])


def snapshot_age_hours(path: str | Path, now: float | None = None) -> float | None:
    file = Path(path)
    if not file.is_file():
        return None
    return ((now if now is not None else time.time()) - file.stat().st_mtime) / 3600


def refresh_snapshot(path: str | Path, *, max_age_hours: float = 20,
                     container: str = DEFAULT_CONTAINER, timeout: int = 240,
                     min_items: int = MIN_ITEMS, run=subprocess.run,
                     now: float | None = None) -> dict:
    """Обновить снимок, если он старше max_age_hours; вернуть краткий отчёт."""
    age = snapshot_age_hours(path, now)
    if age is not None and age < max_age_hours:
        return {"status": "fresh", "age_hours": round(age, 1)}
    try:
        done = run(
            ["docker", "exec", "-i", container, "python", "manage.py", "shell"],
            input=DUMP_SCRIPT.read_text(encoding="utf-8"), capture_output=True,
            text=True, timeout=timeout, check=True,
        )
        rows = parse_dump(done.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return {"status": f"failed: {type(error).__name__}", "kept_old": age is not None}
    if len(rows) < min_items:
        return {"status": f"rejected: {len(rows)} позиций", "kept_old": age is not None}
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, target)
    return {"status": "refreshed", "items": len(rows)}


_BREAKS = re.compile(r"</?(?:p|br|li|ul|ol|div|h[1-6]|tr)[^>]*>", re.I)
_TAGS = re.compile(r"<[^>]+>")
# Разметка на сайте склеивает слова без пробела: «летВысочайшее», «помещений.Благодаря».
_GLUED_WORDS = re.compile(r"([а-яё])([А-ЯЁ][а-яё])")
_NO_SPACE_AFTER_STOP = re.compile(r"([а-яё0-9][.!?])([А-ЯЁA-Z][а-яёa-z])")


def clean_html(raw: str) -> str:
    """Описание на сайте хранится с экранированной разметкой: &lt;p&gt;…&lt;/p&gt;.

    Абзацы и пункты становятся строками, остальные теги отбрасываются.
    """
    newline = chr(10)
    text = html.unescape(raw or "")
    text = _BREAKS.sub(newline, text)
    text = html.unescape(_TAGS.sub("", text)).replace(chr(0xA0), " ")
    text = _GLUED_WORDS.sub(r"\1 \2", text)
    text = _NO_SPACE_AFTER_STOP.sub(r"\1 \2", text)
    return newline.join(line.strip() for line in text.splitlines() if line.strip())


def load_snapshot(path: str | Path) -> list[CatalogItem]:
    """Строки снимка → позиции каталога, годные для розничной ленты."""
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    items = []
    for row in rows:
        category = row.get("category", "")
        name = row.get("title", "").strip()
        price = int(row.get("price") or 0)
        if not (row.get("slug") and row.get("picture") and retail_ok(category, name, price)):
            continue
        prose, attrs = parse_description(clean_html(row.get("description", "")))
        attrs.update({str(k): str(v) for k, v in (row.get("specs") or {}).items()})
        if row.get("is_heat_pump"):
            attrs["Тепловой насос"] = "Да"
            if row.get("heating_min_temp") is not None:
                attrs["Минимальная температура обогрева"] = \
                    f"{row['heating_min_temp']} °C".replace("-", "−")
        items.append(CatalogItem(
            id=ascii_offer_id(row["offer_id"]), url=f"{SITE}/product/{quote(row['slug'])}/",
            price=price, group=refine_group(group_of_item(category, name), attrs), category=category, picture=row["picture"],
            name=name, brand=_brand(name), prose=prose, attrs=attrs,
            pictures=tuple(row.get("pictures") or ()),
        ))
    return items
