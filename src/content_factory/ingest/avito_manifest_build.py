"""Сборка манифеста партии из инвентаря живой выгрузки Avito.

Вход — `state/avito-live-inventory.json` (собран по фиду, Bridge-ценам и очередям
генератора), выход — JSON строго под контракт `avito_manifest.load_manifest`, который
потом проверит хеши и откажет по каждой сомнительной позиции. Здесь только раскладка
полей и отказ по позиции с причиной; ничего не досочиняем и цены не пересчитываем.

Две вещи, которые нельзя взять из инвентаря напрямую:
1. Заголовок. У водонагревателей он безликий («Оборудование Ecostar Smile»), а внутри
   «тёплых полов» лежат терморегуляторы, подписанные тёплым полом. Настоящий тип берём
   из названия модели — это факт из данных, а не догадка.
2. Категория. В инвентаре она числовая и для 119 неоднозначна (пол или регулятор),
   поэтому нормализуем с оглядкой на тип товара."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from content_factory.ingest.avito_manifest import SCHEMA_VERSION, sha256_text

FEED_URL = "https://splithome.ru/static/avito-feed.xml"

# Числовые категории выгрузки → нормализованные имена контракта. Партия владельца:
# масляные радиаторы, водонагреватели, тёплые полы и терморегуляторы. Остальное
# отсеиваем явной причиной, а не молчанием.
CATEGORY_BY_ID = {
    2: "inverter_air_conditioner",
    15: "humidifier",
    19: "fan_heater",
    21: "convector_electric",
    22: "oil_radiator",
    24: "infrared_heater",
    26: "heat_gun",
    27: "air_curtain",
    30: "water_heater",
    37: "breezer",
    40: "ventilation",
    117: "boiler",
    118: "heating_radiator",
    119: "underfloor_heating",
}
# 119 хранит и полы, и регуляторы: различаем по типу товара из модели.
THERMOSTAT_KIND = "терморегулятор"

# «серии» встречается и с латинской c — в данных есть «cерии OMEGA».
_SERIES_RE = re.compile(r"\s+[cс]ерии\s+", re.I)
_FACELESS_TITLE = "оборудование"
# Заголовок прайс-блока внутри описания из фида: «Модели и цены в наличии:», «Цена в наличии:».
_PRICE_BLOCK_RE = re.compile(r"^(модели\s+и\s+цены|цен\w*\s+в\s+наличии)", re.I)
_SPECS_HEAD_RE = re.compile(r"^характеристики\s*:", re.I)
# Объём бака: отдельным числом после кода серии («BWH/S 80»), внутри артикула
# («RWH-OM50-RE») или в форме «SW080». Диапазон отсекает мощности и артикулы.
_VOL_SPACED_RE = re.compile(r"(?:BWH/S|EWH|RWH|RTWGE|INOX-F)\s+(\d{2,3})(?!\S)", re.I)
_VOL_CODE_RE = re.compile(r"\b[A-Z]{2,5}-[A-Z]{1,4}(\d{2,3})-", re.I)
_VOL_SW_RE = re.compile(r"\bSW0?(\d{2,3})\b", re.I)
_VOL_MIN, _VOL_MAX = 10, 500
_LEADING_SERIES_RE = re.compile(r"^[cс]ерии\s+", re.I)
# Объём в заголовке имеет смысл только там, где бак один на позицию.
_VOLUME_CATEGORIES = {"water_heater"}
# «Нет»/«Отсутствует» — перечислять в посте, чего у товара нет, незачем.
_ABSENT_VALUES = {"нет", "отсутствует", "не предусмотрен", "не предусмотрено"}
# Служебные идентификаторы (UUID сертификата и подобное) покупателю ничего не говорят.
_OPAQUE_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
                           r"|[0-9a-f]{24,}", re.I)


def product_kind(model: str, brand: str = "") -> str | None:
    """Тип товара = начало названия модели до бренда или до слова «серии».

    «Водонагреватель Ballu BWH/S 80» → «Водонагреватель». Если ни бренда, ни «серии»
    в модели нет, тип не угадываем — возвращаем None и оставляем исходный заголовок."""
    text = (model or "").strip()
    if not text:
        return None
    cut = None
    if brand:
        idx = text.casefold().find(brand.casefold())
        if idx > 0:
            cut = idx
    match = _SERIES_RE.search(text)
    if match and match.start() > 0:
        cut = match.start() if cut is None else min(cut, match.start())
    if cut is None:
        return None
    return text[:cut].strip(" ,-·") or None


def normalized_category(category_id, kind: str | None) -> str | None:
    base = CATEGORY_BY_ID.get(int(category_id)) if str(category_id).lstrip("-").isdigit() \
        else None
    if base == "underfloor_heating" and (kind or "").casefold().startswith(THERMOSTAT_KIND):
        return "thermostat"
    return base


def display_title(title: str, brand: str, kind: str | None, category: str) -> str:
    """Заголовок поста. Переписываем только там, где исходный заголовок не описывает
    товар: безликое «Оборудование …» и терморегулятор, подписанный «Тёплый пол …»."""
    title = (title or "").strip()
    faceless = title.casefold().startswith(_FACELESS_TITLE)
    mislabelled = category == "thermostat" and not title.casefold().startswith(
        THERMOSTAT_KIND)
    if not kind or not (faceless or mislabelled):
        return title
    tail = title
    if brand:
        idx = title.casefold().find(brand.casefold())
        if idx >= 0:
            tail = title[idx:]
    return f"{kind} {tail}".strip()


def _card_relative(card_path: str, cards_root: str) -> tuple[str | None, str]:
    root = (cards_root or "").replace("\\", "/").rstrip("/")
    path = (card_path or "").replace("\\", "/")
    if not path:
        return None, "нет пути карточки"
    if not path.startswith(root + "/"):
        return None, path
    rel = path[len(root) + 1:]
    return (rel, "") if rel else (None, path)


def tank_volume_l(model: str) -> int | None:
    """Объём бака в литрах из названия модели. → None, если однозначно не читается.

    Для водонагревателя это первое, что смотрит покупатель, и единственный надёжный
    источник — название модели: `BWH/S 80`, `RWH-OM50-RE`, `SW080`, `RTWGE 200`.
    Характеристика «Номинальный объём» из фида НЕ годится: она задана на серию и для
    конкретной модели врёт (у «BWH/S 80 Brief» там стоит 100).

    Неоднозначное не угадываем: `AWH1621/51(50YC)` объёма не даёт."""
    text = model or ""
    for regex in (_VOL_SPACED_RE, _VOL_CODE_RE, _VOL_SW_RE):
        match = regex.search(text)
        if match:
            value = int(match.group(1))
            if _VOL_MIN <= value <= _VOL_MAX:
                return value
    return None


def short_model(model: str, kind: str | None) -> str:
    """Убрать из названия модели тип, который уже стоит в заголовке поста.

    «Водонагреватель Ballu BWH/S 80 Artendo DH» при заголовке «Водонагреватель …» →
    «Ballu BWH/S 80 Artendo DH». Иначе строка модели начинается с того же слова, что и
    название поста."""
    text = (model or "").strip()
    if not kind:
        return text
    if text.casefold().startswith(kind.casefold()):
        text = text[len(kind):].strip(" -–—·,")
        text = _LEADING_SERIES_RE.sub("", text).strip()
    return text or (model or "").strip()


def strip_price_block(text: str) -> str:
    """Убрать из описания блок с перечнем цен.

    Цены в подписи печатаются из `price`/`models` — это единственный источник. Тот же
    перечень внутри описания из фида — застывший снимок: он дублирует таблицу и после
    обновления цен начнёт ей противоречить прямо внутри одного поста."""
    blocks = re.split(r"\n\s*\n", (text or "").replace("\r\n", "\n"))
    kept = [b.strip() for b in blocks if b.strip() and not _PRICE_BLOCK_RE.match(b.strip())]
    return "\n\n".join(kept).strip()


def clean_specs(text: str) -> str:
    """Причесать блок «Характеристики» из фида, не трогая сами значения.

    Из фида приходит выгрузка полей карточки поставщика: там есть отрицания («Инверторная
    технология: Нет»), служебные идентификаторы (UUID пожарного сертификата) и поле,
    буквально названное «УТП», со склеенным через `;` перечнем. Всё это — правда, но не
    текст для поста. Убираем только заведомо ненужные СТРОКИ; значения не переписываем и
    единицы измерения не додумываем."""
    blocks = re.split(r"\n\s*\n", (text or "").replace("\r\n", "\n"))
    out = []
    for block in blocks:
        block = block.strip()
        if not block or not _SPECS_HEAD_RE.match(block):
            if block:
                out.append(block)
            continue
        head, *rows = block.split("\n")
        kept = []
        for row in rows:
            body = row.lstrip("•·-— ").strip()
            key, _, value = body.partition(":")
            key, value = key.strip().rstrip("_ ").strip(), value.strip()
            if not value:
                continue
            if value.casefold() in _ABSENT_VALUES:
                continue
            if _OPAQUE_ID_RE.fullmatch(value):
                continue
            if key.casefold() == "утп":
                kept += [f"• {part.strip()}" for part in value.split(";") if part.strip()]
                continue
            kept.append(f"• {key}: {value}")
        if kept:
            out.append("\n".join([head, *kept]))
    return "\n\n".join(out).strip()


def _models(item) -> list[dict]:
    rows = []
    for member in item.get("members") or []:
        model = str(member.get("model") or "").strip()
        price = member.get("final_price")
        if model and isinstance(price, int) and not isinstance(price, bool) and price > 0:
            rows.append({"model": model, "price": price})
    return rows


def _skip(key, reason, detail=""):
    return {"key": key, "reason": reason, "detail": str(detail)}


def build_item(item: dict, cards_root: str) -> tuple[dict | None, dict | None]:
    """Одна позиция инвентаря → позиция манифеста. Fail closed с причиной."""
    key = str(item.get("supplier_sku") or item.get("source_id") or "?")
    card_path = item.get("generated_card_path")
    if not card_path:
        return None, _skip(key, "no_generated_card", "карточка не сгенерирована")
    sha = str(item.get("generated_card_sha256") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        return None, _skip(key, "card_sha_missing", sha)
    rel, bad = _card_relative(card_path, cards_root)
    if rel is None:
        return None, _skip(key, "card_outside_cards_root", bad)

    stock = item.get("stock")
    if not isinstance(stock, int) or stock <= 0:
        return None, _skip(key, "not_in_stock", stock)

    price = item.get("price")
    if not isinstance(price, int) or isinstance(price, bool) or price <= 0:
        return None, _skip(key, "no_price", price)

    models = _models(item)
    kind = product_kind(models[0]["model"] if models else "", item.get("brand") or "")
    category = normalized_category(item.get("category_id"), kind)
    if not category:
        return None, _skip(key, "category_not_in_batch", item.get("category_id"))

    raw_kind = str(item.get("price_kind") or "").lower()
    if raw_kind == "from":
        if len(models) < 2:
            return None, _skip(key, "series_needs_two_models", len(models))
        if price != min(m["price"] for m in models):
            return None, _skip(key, "series_price_mismatch",
                               f"price={price} min={min(m['price'] for m in models)}")
        price_kind = "series_from"
    elif raw_kind == "exact":
        price_kind = "exact"
        # У точной цены контракт разрешает не больше одной модели и ровно ту же цену.
        if len(models) != 1 or models[0]["price"] != price:
            models = []
    else:
        return None, _skip(key, "price_kind_unknown", raw_kind)

    text = clean_specs(strip_price_block(item.get("description_override")
                                         or item.get("description") or ""))
    if not text:
        return None, _skip(key, "no_usp", "нет описания")
    usp_kind = "generator_override" if item.get("description_override") \
        else "feed_description"

    title = display_title(item.get("title") or "", item.get("brand") or "", kind, category)
    if not title:
        return None, _skip(key, "title_missing", "")
    # Объём бака — в заголовок, но только когда бак на позицию один: у серии объёмы
    # разные, и общий литраж в шапке был бы неправдой (там их показывают модели).
    if category in _VOLUME_CATEGORIES and price_kind == "exact" and models:
        volume = tank_volume_l(models[0]["model"])
        if volume:
            title = f"{title}, {volume} л"
    # Тип товара уже стоит в заголовке — в названиях моделей он только мешает.
    models = [{**row, "model": short_model(row["model"], kind)} for row in models]

    job = item.get("generator_job") or {}
    card_job = item.get("card_job") or {}
    built = {
        "source_id": str(item.get("supplier_sku") or "").strip(),
        "sku": str(item.get("supplier_sku") or "").strip(),
        "series_key": str(item.get("series_key") or "").strip(),
        "category": category,
        "availability": "in_stock",
        # Бренд уже внутри заголовка: манифест добавляет его только к title без бренда.
        "title": title,
        "brand": "",
        "card": {"path": rel, "sha256": sha, "provenance": "generated",
                 "generator": "fotogen",
                 "job_id": str(job.get("id") or card_job.get("key") or "")},
        "usp": {"kind": usp_kind, "text": text,
                "source_ref": f"avito-live-inventory.json#{item.get('source_id') or key}",
                "sha256": sha256_text(text)},
        "price": {"final": price, "currency": str(item.get("currency") or "RUB").upper(),
                  "kind": price_kind, "already_marked_up": True},
        "models": models,
    }
    return built, None


def build_manifest(inventory: dict, *, cards_root: str, batch_id: str) -> tuple[dict, list]:
    """Инвентарь → (манифест, пропущенные с причинами)."""
    feed_sha = str(inventory.get("feed_sha256") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", feed_sha):
        raise ValueError("в инвентаре нет feed_sha256 — версия источника цен не зафиксирована")
    items, skipped, seen = [], [], set()
    for raw in inventory.get("items") or []:
        built, skip = build_item(raw, cards_root)
        if skip:
            skipped.append(skip)
            continue
        if built["source_id"] in seen:
            raise ValueError(f"дубль source_id в инвентаре: {built['source_id']}")
        seen.add(built["source_id"])
        items.append(built)
    reasons = {}
    for row in skipped:
        reasons[row["reason"]] = reasons.get(row["reason"], 0) + 1
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "batch_id": batch_id,
        "cards_root": cards_root,
        "feed": {"url": FEED_URL, "sha256": feed_sha,
                 "checked_at": inventory.get("checked_at") or ""},
        # Причины отсева едут вместе с партией: отчёт владельцу печатает импортёр,
        # и без этого позиции, отвалившиеся здесь, исчезли бы из отчёта молча.
        "excluded": {"count": len(skipped),
                     "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1]))},
        "items": items,
    }
    return manifest, skipped


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Собрать манифест партии Avito из инвентаря живой выгрузки.")
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--cards-root", required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    inventory = json.loads(Path(args.inventory).read_text(encoding="utf-8"))
    manifest, skipped = build_manifest(inventory, cards_root=args.cards_root,
                                       batch_id=args.batch_id)
    Path(args.out).write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    reasons = {}
    for row in skipped:
        reasons[row["reason"]] = reasons.get(row["reason"], 0) + 1
    print(json.dumps({"items": len(manifest["items"]), "skipped": len(skipped),
                      "reasons": reasons, "out": args.out}, ensure_ascii=False))


if __name__ == "__main__":
    main()
