"""Детерминированная ревизия поста перед публикацией — БЕЗ LLM (решение владельца).

Набор правил-проверок: цена в границах, есть ТТХ, карточка-файл есть и не пустой,
описание непустое/в лимите/без стоп-слов, бренд и тип заполнены. Возвращает
(ok, reasons[]). Не прошло → товар в held + алерт владельцу (на уровне оркестратора)."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ReviewItem:
    """Что ревизуем: финальные данные поста (после цены, описания, карточки)."""
    price: int | None
    caption: str
    attrs: dict                 # ТТХ товара
    card_path: str | None       # путь к файлу карточки на диске
    brand: str
    category_id: int | None     # тип определяется по категории


def _has_specs(attrs: dict) -> bool:
    """Есть ли хотя бы одна непустая ТТХ (с учётом мусора '( - )' как пустоты)."""
    for v in (attrs or {}).values():
        if (v or "").replace("( - )", "").strip():
            return True
    return False


def _card_ok(path: str | None) -> tuple[bool, str]:
    if not path:
        return False, "нет карточки"
    p = Path(path)
    if not p.exists():
        return False, "файл карточки не найден"
    if p.stat().st_size == 0:
        return False, "карточка пустая"
    return True, ""


def review(item: ReviewItem, cfg, stop_words=()) -> tuple[bool, list[str]]:
    """Прогон правил. cfg — ReviewConfig (price_min/max, require_specs/card, caption_max).
    stop_words — список стоп-слов (из content-конфига); проверяется как страховка."""
    reasons: list[str] = []

    # цена > 0 и в границах
    if item.price is None or item.price <= 0:
        reasons.append("цена ≤ 0 или отсутствует")
    else:
        if item.price < cfg.price_min:
            reasons.append(f"цена ниже минимума ({item.price} < {cfg.price_min})")
        if item.price > cfg.price_max:
            reasons.append(f"цена выше максимума ({item.price} > {cfg.price_max})")

    # хотя бы одна ТТХ
    if cfg.require_specs and not _has_specs(item.attrs):
        reasons.append("нет ТТХ")

    # карточка-файл существует и не пустой
    if cfg.require_card:
        ok_card, why = _card_ok(item.card_path)
        if not ok_card:
            reasons.append(why)

    # описание: непустое, в лимите, без стоп-слов
    cap = item.caption or ""
    if not cap.strip():
        reasons.append("описание пустое")
    elif len(cap) > cfg.caption_max:
        reasons.append(f"описание длиннее лимита ({len(cap)} > {cfg.caption_max})")
    found = [w for w in (stop_words or []) if w and w.lower() in cap.lower()]
    if found:
        reasons.append("стоп-слова: " + ", ".join(found))

    # бренд и тип заполнены
    if not (item.brand or "").strip():
        reasons.append("нет бренда")
    if item.category_id is None:
        reasons.append("нет типа (категории)")

    return (not reasons, reasons)
