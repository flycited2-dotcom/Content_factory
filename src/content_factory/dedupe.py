"""Каноническая дедупликация товаров и публикаций между источниками."""
from __future__ import annotations

import hashlib
import html
import re


_TAG_RE = re.compile(r"<[^>]+>")
_URL_RE = re.compile(r"https?://\S+|(?:www\.)?[a-z0-9-]+\.(?:ru|com|рф)\S*", re.I)
_PHONE_RE = re.compile(r"(?:\+?7|8)[\s()\-\d]{9,}")
_PRICE_RE = re.compile(r"\b[\d\s]{2,}\s*(?:₽|руб\.?|р\.)", re.I)
_TOKEN_RE = re.compile(r"[a-zа-яё0-9]+", re.I)

_GENERIC_PRODUCT_WORDS = {
    "кондиционер", "кондиционеры", "настенный", "настенная", "настенные",
    "сплит", "система", "системы", "инверторный", "инверторная", "инверторные",
    "классический", "классическая", "классические",
    "бытовой", "бытовая", "модель", "серия", "серии", "стабилизатор",
    "напряжения", "однофазный", "однофазная", "источник", "бесперебойного",
    "питания", "ибп", "рекуператор", "вентиляция", "приточно", "вытяжная",
    "тепловой", "насос", "квт", "вт", "ва", "бте", "btu", "до", "для",
}

_BOILERPLATE_LINES = (
    "подберём модель", "подберем модель", "напишите или позвоните",
    "позвоните или оставьте", "телефон", "заказать", "utm_",
)


def normalize_text(value: str) -> str:
    text = html.unescape(_TAG_RE.sub(" ", value or "")).casefold().replace("ё", "е")
    text = _URL_RE.sub(" ", text)
    text = _PHONE_RE.sub(" ", text)
    text = _PRICE_RE.sub(" ", text)
    lines = [line for line in text.splitlines()
             if not any(marker in line for marker in _BOILERPLATE_LINES)]
    return " ".join(_TOKEN_RE.findall("\n".join(lines)))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_product_identity(*, source_key: str, caption: str,
                               category: str, brand: str) -> str:
    """Стабильная модель товара, не зависящая от поставщика и цены."""
    title = next((line.strip() for line in (caption or "").splitlines() if line.strip()), "")
    brand_tokens = set(_TOKEN_RE.findall((brand or "").casefold().replace("ё", "е")))
    tokens = [token for token in _TOKEN_RE.findall(title.casefold().replace("ё", "е"))
              if token not in _GENERIC_PRODUCT_WORDS and token not in brand_tokens]
    if not tokens:
        source_parts = re.split(r"[|:/_-]+", (source_key or "").casefold())
        tokens = [part for part in source_parts if len(part) >= 2
                  and part not in _GENERIC_PRODUCT_WORDS and part not in brand_tokens]
    # Порядок в названии модели значим; дублирующиеся слова и служебный хвост — нет.
    signature = " ".join(dict.fromkeys(tokens[:16]))
    raw = "|".join((category.casefold(), " ".join(sorted(brand_tokens)), signature))
    return f"product:{_digest(raw)[:24]}"


def post_fingerprint(*, source_key: str, caption: str, category: str,
                     brand: str, content_type: str) -> tuple[str, str]:
    normalized = normalize_text(caption)
    if content_type == "product":
        key = canonical_product_identity(
            source_key=source_key, caption=caption, category=category, brand=brand,
        )
    else:
        key = f"post:{_digest(f'{content_type}|{normalized}')[:24]}"
    return key, normalized


def text_similarity(left: str, right: str) -> float:
    a, b = set((left or "").split()), set((right or "").split())
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
