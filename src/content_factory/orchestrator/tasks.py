"""Модель задачи владельца и выбор товаров под задачу.

Task — что/сколько/когда/каким стилем публиковать. Единица публикации — СЕРИЯ
(SeriesGroup), как в avito-bridge: карточка генерится на серию, пост — на серию.
`confirm` — пилотный режим human-in-the-loop (ждать OK перед публикацией)."""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class Task:
    id: str
    filter: dict                              # {categories:[...], source:..., series_whitelist:[...]}
    count: int                                # сколько серий за один слот расписания
    mode: str = "mcp"                         # стиль карточки
    schedule: list[str] = field(default_factory=list)  # локальные времена "YYYY-MM-DD HH:MM"
    channel: str = ""                         # пусто = из конфига
    confirm: bool = False                     # ждать подтверждения владельца перед постом


def matches(group, filter: dict) -> bool:
    """Подходит ли серия под фильтр задачи. Пустой фильтр — берём всё."""
    cats = filter.get("categories")
    if cats and group.category_id not in cats:
        return False
    src = filter.get("source")
    if src and group.source != src:
        return False
    wl = filter.get("series_whitelist")
    if wl and group.key not in wl:
        return False
    return True


def select_items(groups, filter: dict, published_keys: set, count: int) -> list:
    """До `count` серий под фильтр, ещё не опубликованных (анти-дубль по group.key).
    Порядок групп сохраняется (детерминированно)."""
    out = []
    for g in groups:
        if g.key in published_keys:
            continue
        if not matches(g, filter):
            continue
        out.append(g)
        if len(out) >= count:
            break
    return out
