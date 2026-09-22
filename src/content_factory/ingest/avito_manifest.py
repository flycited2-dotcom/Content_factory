"""Контракт входа для готовых карточек Avito: манифест партии + строгая валидация.

Импортёр НЕ ходит в генератор карточек и НЕ угадывает соответствия: всё, что нужно посту,
приходит одним JSON-манифестом с точными ключами и хешами. Любое отсутствующее или
неоднозначное соответствие (карточка, УТП, цена, категория, наличие) — отказ ПО ПОЗИЦИИ
с причиной; структурные проблемы партии (версия схемы, дубли идентичности) — отказ всей
партии. Фото из XML-фида карточкой не считается: нужен `provenance='generated'`.

Две разные идентичности (решение по итогам сверки с Codex):
- `product_key` = `avito|<source_id>` — идентичность ТОВАРА, ключ в ConfirmStore. В него
  не входят хеши: иначе смена цены/УТП создала бы новый ключ и обошла защиту
  pending/rejected/published.
- `import_key` — идентичность РЕВИЗИИ (source_id + хеши карточки/УТП/цены/фида). Живёт
  только в журнале импортёра и служит защитой от повторной отправки той же ревизии."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1
MAX_CARD_BYTES = 10 * 1024 * 1024          # лимит sendPhoto
KEY_PREFIX = "avito"

# Категории партии (нормализованные id → русское название для подписи).
ALLOWED_CATEGORIES = {
    "oil_radiator": "Масляный радиатор",
    "heat_gun_electric": "Электрическая тепловая пушка",
    "heat_gun_gas": "Газовая тепловая пушка",
    "heat_gun_diesel": "Дизельная тепловая пушка",
    "underfloor_heating": "Тёплый пол",
    "thermostat": "Терморегулятор",
    "water_heater": "Водонагреватель",
    "heat_pump": "Тепловой насос",
    "inverter_air_conditioner": "Инверторный кондиционер",
    "convector_electric": "Электрический конвектор",
    "breezer": "Бризер",
    "ventilation": "Вентиляционная установка",
    "humidifier": "Увлажнитель воздуха",
    # Тип пушки (электро/газ/дизель) виден в названии модели, которое печатает пост;
    # угадывать его из категории каталога нельзя — там все три лежат вместе.
    "heat_gun": "Тепловая пушка",
    "fan_heater": "Тепловентилятор",
    "infrared_heater": "Инфракрасный обогреватель",
    "air_curtain": "Тепловая завеса",
    "boiler": "Отопительный котёл",
    "heating_radiator": "Радиатор отопления",
}
# Владелец просил «проверить также» — пропускаем, но помечаем отдельной группой,
# чтобы решение по ним принималось осознанно, а не растворилось в общей партии.
REVIEW_REQUIRED_CATEGORIES = {"convector_electric", "breezer"}
DENIED_CATEGORIES = {
    "generator": "генераторы не входят в эту партию",
    "voltage_stabilizer": "стабилизаторы не входят в эту партию",
}

ALLOWED_PROVENANCE = "generated"
ALLOWED_USP_KINDS = {"generator_override", "feed_description"}
ALLOWED_AVAILABILITY = {"in_stock"}
PRICE_KINDS = {"exact", "series_from"}

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
# Блок УТП считаем avito-специфичным призывом, только если он одновременно упоминает
# площадку и является призывом. Упоминание без призыва — неоднозначность (fail closed),
# чтобы не вырезать факт из середины предложения.
_AVITO_MARKERS = ("avito", "авито")
_CALL_MARKERS = ("напиш", "пиш", "звон", "обращайт", "заявк", "сообщени", "свяж",
                 "закаж", "заказыв", "откликн")       # без «чат»: оно внутри «начать»


class ManifestError(ValueError):
    """Структурная ошибка манифеста: партию обрабатывать нельзя."""


@dataclass(frozen=True)
class ManifestSkip:
    key: str
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class ModelPrice:
    model: str
    price: int


@dataclass(frozen=True)
class ManifestItem:
    product_key: str
    import_key: str
    source_id: str
    sku: str
    series_key: str
    category: str
    category_label: str
    review_required: bool
    display_name: str
    card_path: Path                 # абсолютный путь ОРИГИНАЛА (копию делает импортёр)
    card_sha256: str
    card_kind: str                  # png | jpeg
    card_generator: str
    card_job_id: str
    usp_text: str                   # очищенный от avito-призывов
    usp_kind: str
    usp_source_ref: str
    usp_sha256: str                 # хеш ИСХОДНОГО текста
    price_final: int
    price_kind: str
    currency: str
    models: tuple[ModelPrice, ...] = ()

    @property
    def price_from(self) -> bool:
        """«от» показываем только у серии с ≥2 моделями с ценами."""
        return self.price_kind == "series_from" and len(self.models) >= 2


@dataclass
class LoadedManifest:
    batch_id: str
    feed: dict
    cards_root: Path
    items: list[ManifestItem] = field(default_factory=list)
    skipped: list[ManifestSkip] = field(default_factory=list)
    # Сводка отсева, случившегося ДО манифеста (на сборке партии): нужна отчёту.
    excluded: dict = field(default_factory=dict)


def sha256_bytes(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes((text or "").encode("utf-8"))


def image_kind(head: bytes) -> str | None:
    """Тип изображения по сигнатуре файла, а не по расширению."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    return None


def product_key_for(source_id: str) -> str:
    return f"{KEY_PREFIX}|{source_id}"


def import_key_for(source_id: str, card_sha: str, usp_sha: str, price_final: int,
                   price_kind: str, currency: str, feed_sha: str) -> str:
    """Идентичность ревизии: меняются карточка/УТП/цена/фид — меняется ключ ревизии."""
    raw = "\x1f".join([source_id, card_sha, usp_sha, str(price_final), price_kind,
                       currency, feed_sha])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def clean_usp(text: str) -> tuple[str, str | None]:
    """Убрать avito-специфичные призывы ЦЕЛЫМИ блоками. → (текст, причина отказа).

    Ничего не переписываем внутри блока: блок либо целиком призыв (выкидываем), либо
    упоминает площадку без призыва (неоднозначно — отказываем по позиции)."""
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    raw = _TAG_RE.sub(" ", raw)                       # HTML из описаний фида не пробрасываем
    blocks = [_WS_RE.sub(" ", b).strip() for b in re.split(r"\n\s*\n", raw)]
    kept: list[str] = []
    for block in blocks:
        if not block:
            continue
        low = block.casefold()
        has_platform = any(m in low for m in _AVITO_MARKERS)
        if not has_platform:
            kept.append(block)
            continue
        if any(m in low for m in _CALL_MARKERS):
            continue                                   # призыв писать на площадку — вон
        return "", "usp_platform_mention_ambiguous"
    result = "\n\n".join(kept).strip()
    if not result:
        return "", "usp_empty_after_cleanup"
    return result, None


def _need(raw: dict, key: str) -> str:
    value = raw.get(key)
    return str(value).strip() if value is not None else ""


def _resolve_card(cards_root: Path, rel: str) -> tuple[Path | None, str]:
    """Путь карточки строго относительный и строго внутри cards_root."""
    if not rel:
        return None, "card_path_missing"
    if "://" in rel:
        return None, "card_path_is_url"
    if rel.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", rel):
        return None, "card_path_not_relative"
    if ".." in Path(rel.replace("\\", "/")).parts:
        return None, "card_path_traversal"
    candidate = cards_root / rel.replace("\\", "/")
    if candidate.is_symlink():
        return None, "card_path_symlink"
    resolved = candidate.resolve()
    if not resolved.is_relative_to(cards_root.resolve()):
        return None, "card_path_escape"
    if not resolved.is_file():
        return None, "card_file_missing"
    return resolved, ""


def _validate_card(raw: dict, cards_root: Path) -> tuple[dict | None, ManifestSkip | None]:
    key = "?"
    card = raw.get("card") or {}
    provenance = _need(card, "provenance")
    generator = _need(card, "generator")
    if provenance != ALLOWED_PROVENANCE:
        return None, ManifestSkip(key, "card_not_generated", provenance or "нет provenance")
    if not generator or generator.casefold() in {"feed_photo", "supplier", "unknown", "xml"}:
        return None, ManifestSkip(key, "card_not_generated", generator or "нет generator")
    sha = _need(card, "sha256").lower()
    if not _SHA_RE.match(sha):
        return None, ManifestSkip(key, "card_sha_invalid", sha)
    path, why = _resolve_card(cards_root, _need(card, "path"))
    if path is None:
        return None, ManifestSkip(key, why, _need(card, "path"))
    size = path.stat().st_size
    if size == 0:
        return None, ManifestSkip(key, "card_empty", str(path))
    if size > MAX_CARD_BYTES:
        return None, ManifestSkip(key, "card_too_large", f"{size} байт")
    blob = path.read_bytes()
    kind = image_kind(blob[:16])
    if kind is None:
        return None, ManifestSkip(key, "card_not_an_image", str(path))
    if sha256_bytes(blob) != sha:
        return None, ManifestSkip(key, "card_sha_mismatch", str(path))
    return {"path": path, "sha256": sha, "kind": kind, "generator": generator,
            "job_id": _need(card, "job_id")}, None


def _validate_price(raw: dict) -> tuple[dict | None, ManifestSkip | None]:
    key = "?"
    price = raw.get("price") or {}
    currency = _need(price, "currency").upper()
    kind = _need(price, "kind")
    if currency != "RUB":
        return None, ManifestSkip(key, "price_currency", currency or "нет валюты")
    if kind not in PRICE_KINDS:
        return None, ManifestSkip(key, "price_kind_unknown", kind or "нет kind")
    if price.get("already_marked_up") is not True:
        # Наценка применяется в источнике; повторно её не считаем и не угадываем.
        return None, ManifestSkip(key, "price_not_final", "already_marked_up != true")
    final = price.get("final")
    if not isinstance(final, int) or isinstance(final, bool) or final <= 0:
        return None, ManifestSkip(key, "price_invalid", str(final))
    models: list[ModelPrice] = []
    for row in (raw.get("models") or []):
        name = _need(row, "model")
        value = row.get("price")
        if not name:
            return None, ManifestSkip(key, "model_name_missing", "")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            models.append(ModelPrice(name, value))
    if kind == "series_from":
        if len(models) < 2:
            return None, ManifestSkip(key, "series_needs_two_priced_models", str(len(models)))
        if final != min(m.price for m in models):
            return None, ManifestSkip(key, "series_from_price_mismatch",
                                      f"final={final} min={min(m.price for m in models)}")
    elif models and (len(models) > 1 or models[0].price != final):
        return None, ManifestSkip(key, "exact_price_mismatch", f"final={final}")
    models.sort(key=lambda m: (m.price, m.model))
    return {"final": final, "kind": kind, "currency": currency,
            "models": tuple(models)}, None


def _validate_usp(raw: dict) -> tuple[dict | None, ManifestSkip | None]:
    key = "?"
    usp = raw.get("usp") or {}
    kind = _need(usp, "kind")
    if kind not in ALLOWED_USP_KINDS:
        return None, ManifestSkip(key, "usp_kind_unknown", kind or "нет kind")
    source_ref = _need(usp, "source_ref")
    if not source_ref:
        return None, ManifestSkip(key, "usp_source_ref_missing", "")
    text = usp.get("text")
    if not isinstance(text, str) or not text.strip():
        return None, ManifestSkip(key, "usp_empty", "")
    sha = _need(usp, "sha256").lower()
    if not _SHA_RE.match(sha):
        return None, ManifestSkip(key, "usp_sha_invalid", sha)
    if sha256_text(text) != sha:
        return None, ManifestSkip(key, "usp_sha_mismatch", "")
    clean, why = clean_usp(text)
    if why:
        return None, ManifestSkip(key, why, "")
    return {"text": clean, "kind": kind, "source_ref": source_ref, "sha256": sha}, None


def _display_name(raw: dict) -> str:
    title = _need(raw, "title")
    brand = _need(raw, "brand")
    if not title:
        return ""
    if brand and not title.casefold().startswith(brand.casefold()):
        return f"{brand} {title}"
    return title


def build_item(raw: dict, cards_root: Path, feed_sha: str) -> tuple[ManifestItem | None,
                                                                    ManifestSkip | None]:
    """Валидация одной позиции. Fail closed: любая неполнота — отказ с причиной."""
    source_id = _need(raw, "source_id")
    key = product_key_for(source_id) if source_id else "?"
    if not source_id or re.search(r"\s", source_id):
        return None, ManifestSkip(key, "source_id_invalid", source_id)
    category = _need(raw, "category").lower()
    if category in DENIED_CATEGORIES:
        return None, ManifestSkip(key, "category_denied", DENIED_CATEGORIES[category])
    if category not in ALLOWED_CATEGORIES:
        return None, ManifestSkip(key, "category_unknown", category or "нет категории")
    availability = _need(raw, "availability").lower()
    if availability not in ALLOWED_AVAILABILITY:
        return None, ManifestSkip(key, "not_in_stock", availability or "нет availability")
    sku = _need(raw, "sku")
    series_key = _need(raw, "series_key")
    if not sku and not series_key:
        return None, ManifestSkip(key, "identity_missing", "нет ни sku, ни series_key")
    display_name = _display_name(raw)
    if not display_name:
        return None, ManifestSkip(key, "title_missing", "")

    card, skip = _validate_card(raw, cards_root)
    if skip:
        return None, ManifestSkip(key, skip.reason, skip.detail)
    price, skip = _validate_price(raw)
    if skip:
        return None, ManifestSkip(key, skip.reason, skip.detail)
    usp, skip = _validate_usp(raw)
    if skip:
        return None, ManifestSkip(key, skip.reason, skip.detail)

    item = ManifestItem(
        product_key=key,
        import_key=import_key_for(source_id, card["sha256"], usp["sha256"],
                                  price["final"], price["kind"], price["currency"], feed_sha),
        source_id=source_id, sku=sku, series_key=series_key,
        category=category, category_label=ALLOWED_CATEGORIES[category],
        review_required=category in REVIEW_REQUIRED_CATEGORIES,
        display_name=display_name,
        card_path=card["path"], card_sha256=card["sha256"], card_kind=card["kind"],
        card_generator=card["generator"], card_job_id=card["job_id"],
        usp_text=usp["text"], usp_kind=usp["kind"], usp_source_ref=usp["source_ref"],
        usp_sha256=usp["sha256"],
        price_final=price["final"], price_kind=price["kind"], currency=price["currency"],
        models=price["models"],
    )
    return item, None


def load_manifest(path, *, cards_root=None) -> LoadedManifest:
    """Прочитать и проверить манифест партии. Структурные проблемы → ManifestError."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestError(f"манифест не читается: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError("манифест должен быть объектом")
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ManifestError(f"schema_version={version!r}, поддерживается {SCHEMA_VERSION}")
    batch_id = str(data.get("batch_id") or "").strip()
    if not re.fullmatch(r"[\w.-]{1,64}", batch_id or ""):
        raise ManifestError("batch_id обязателен и должен быть [\\w.-]{1,64}")
    feed = data.get("feed") or {}
    feed_sha = str(feed.get("sha256") or "").strip().lower()
    if not _SHA_RE.match(feed_sha):
        raise ManifestError("feed.sha256 обязателен (фиксирует версию источника цен)")
    root = Path(cards_root or data.get("cards_root") or "")
    if not root.is_absolute():
        raise ManifestError("cards_root должен быть абсолютным путём")
    if not root.is_dir():
        raise ManifestError(f"cards_root не найден: {root}")
    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ManifestError("items пуст")

    seen_source, seen_sku = set(), set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise ManifestError("элемент items должен быть объектом")
        sid = _need(raw, "source_id")
        sku = _need(raw, "sku")
        if sid and sid in seen_source:
            raise ManifestError(f"дубль source_id: {sid}")
        if sku and sku in seen_sku:
            raise ManifestError(f"дубль sku: {sku}")
        seen_source.add(sid)
        if sku:
            seen_sku.add(sku)

    excluded = data.get("excluded")
    loaded = LoadedManifest(batch_id=batch_id, feed=dict(feed), cards_root=root,
                            excluded=dict(excluded) if isinstance(excluded, dict) else {})
    for raw in raw_items:
        item, skip = build_item(raw, root, feed_sha)
        if item is not None:
            loaded.items.append(item)
        else:
            loaded.skipped.append(skip)
    return loaded
