"""Card-aware подбор фото: если для товара есть СГЕНЕРИРОВАННАЯ уникальная карточка
(фотоагент кладёт её на сервер в папку как `{nc_code}.jpg`), используем её вместо
общего фото поставщика. Это снимает блок Avito «повторное размещение» по фото
(модели одной серии у поставщика делят одно фото).

Контракт с фотоагентом: имя файла = ключ товара (часть supplier_sku после ':',
т.е. nc_code/артикул), приведённый к безопасному виду (`card_key`). Папка и
публичный URL — в config (`cards`)."""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote
from content_factory.models import Offer


@dataclass
class CardConfig:
    enabled: bool = False
    dir: str = ""               # путь к папке с карточками на сервере
    base_url: str = ""          # публичный HTTPS-префикс этой папки
    exts: list = field(default_factory=lambda: [".jpg", ".jpeg", ".png"])
    require_for_publish: bool = False   # публиковать серию ТОЛЬКО при наличии уникальной карточки


def has_card(offer: Offer, cfg: CardConfig) -> bool:
    """Есть ли для товара сгенерированная уникальная карточка на сервере."""
    if not (cfg.enabled and cfg.dir):
        return False
    key = card_key(offer.supplier_sku)
    return any((Path(cfg.dir) / f"{key}{ext}").exists() for ext in cfg.exts)


def card_key(supplier_sku: str) -> str:
    """Ключ файла карточки = код товара (часть supplier_sku после ':', т.е. nc_code).
    Кириллицу СОХРАНЯЕМ (контракт прост: «назови файл кодом товара»); заменяем только
    пробелы и слэши, опасные для имени файла."""
    raw = supplier_sku.split(":", 1)[-1].strip()
    return re.sub(r"[\\/\s]+", "_", raw)


def mode_for(obj, modes_by_category: dict | None, default: str) -> str:
    """Стиль карточки (mode) по категории товара — детерминированно, без ИИ.
    obj — SeriesGroup или Offer (у обоих есть category_id). Режим = справочник
    category_id→mode (проставлено сайтом, не догадка). Неизвестная/пустая категория
    → default (вызывающий код может отдельно заметить это и алертить владельцу)."""
    cat = getattr(obj, "category_id", None)
    return (modes_by_category or {}).get(cat, default)


def build_modes_map(groups, modes_by_category: dict | None, default: str,
                    overrides: dict | None = None) -> tuple[dict, set]:
    """Карта g.key→mode для пачки серий. Приоритет: ручной override (overrides по ключу
    серии — явное решение владельца) → авто-выбор по категории (mode_for). Возвращает
    (modes, unknown): unknown — множество category_id без записи в карте (для предупреждения
    владельцу, чтобы дописал маппинг); считается только если карта категорий задана (пустая
    карта = авто-выбор не настроен → не алертим, всё идёт на default)."""
    overrides = overrides or {}
    modes, unknown = {}, set()
    for g in groups:
        if g.key in overrides:
            modes[g.key] = overrides[g.key]
            continue
        modes[g.key] = mode_for(g, modes_by_category, default)
        cat = getattr(g, "category_id", None)
        if modes_by_category and cat not in modes_by_category:
            unknown.add(cat)
    return modes, unknown


def resolve_photos(offer: Offer, cfg: CardConfig) -> list[str]:
    """URL фото для объявления: сгенерированная карточка (если есть) — иначе фото поставщика.
    Если карточка найдена — возвращаем ТОЛЬКО её (чтобы не тащить общее фото-дубль серии).
    URL процент-кодируется (имя файла может быть кириллическим)."""
    if cfg.enabled and cfg.dir:
        key = card_key(offer.supplier_sku)
        for ext in cfg.exts:
            if (Path(cfg.dir) / f"{key}{ext}").exists():
                return [f"{cfg.base_url.rstrip('/')}/{quote(key + ext)}"]
    return list(offer.photos)
