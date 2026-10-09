"""Фото и характеристики АРУ для постов (excel-конвейер).

PriceItem не несёт фото и характеристик, а в aru-catalog.json они есть. Для выбранного
товара кладём готовые УТП и оригинал фото в research_cache — тогда tick пропускает
этап research (ChatGPT) и сразу делает карточку. Текст — детерминированный, из
характеристик поставщика (без LLM). Цена и наценка здесь НЕ считаются: их даёт слот aru.
"""
from __future__ import annotations
import collections
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from content_factory.ingest.aru_account import read_snapshot

_THUMB_RE = re.compile(
    r"/products/(?:\d+/webp/)?(?P<path>\d+/\d+/\d+/images/\d+)/(?P<name>\d+)\.\d+\.(?:webp|jpe?g)$")
_OG_RE = re.compile(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"')
_SKIP_SPECS = {"бренд"}          # бренд уже стоит в названии карточки
_GENERIC_KEY = "характеристика"
_UUID_KEY_RE = re.compile(r"^[0-9a-f]{8}_[0-9a-f_]+$")
_MAX_UTP_LINES = 8
_MIN_PHOTO_BYTES = 5000
_ATTEMPTS = 3                    # aru.ooo периодически подвисает — один сбой не повод для research


@dataclass(frozen=True)
class AruDetail:
    id: str
    name: str
    brand: str
    url: str
    photo_url: str | None
    specs: dict
    supplier_updated_at: str
    cache_key: str


def original_photo_url(url: str) -> str | None:
    """Список отдаёт миниатюру 180 px: .../products/00/webp/<путь>/<id>.180.webp (60%) или
    .../products/<путь>/<id>.180.jpg (40%); большой JPEG (~800×970) лежит рядом:
    .../products/<путь>/<id>.970.jpg."""
    m = _THUMB_RE.search(url or "")
    if not m:
        return None
    head = url[:m.start()]
    return f"{head}/products/{m['path']}/{m['name']}.970.jpg"


def utp_from_specs(specs: dict) -> str:
    """Характеристики АРУ скудные: у большинства единственный ключ «характеристика»
    (значение — суть), встречаются флаги «Да» и служебные UUID-ключи."""
    lines = []
    for key, value in (specs or {}).items():
        key, value = str(key).strip(), " ".join(str(value).split())
        if not key or not value or key.casefold() in _SKIP_SPECS or _UUID_KEY_RE.match(key):
            continue
        if key.casefold() == _GENERIC_KEY:
            lines.append(f"✓ {value}")
        elif value.casefold() in {"да", "yes"}:
            lines.append(f"✓ {key}")
        else:
            lines.append(f"✓ {key}: {value}")
    return "\n".join(lines[:_MAX_UTP_LINES])


def _cache_key(brand: str, name: str) -> str:
    from content_factory.ingest.excel_price import extract_model
    return f"{brand.strip().lower()}|{extract_model(name, brand).strip().lower()}"


def load_aru_details(prices_dir) -> dict[tuple[str, str], AruDetail]:
    """(бренд.casefold, название) → детали. Неоднозначные строки не отдаём: у ~5% товаров
    совпадают бренд+название или ключ кэша бренд|модель — фото чужого товара подмешивать нельзя."""
    data = read_snapshot(Path(prices_dir))
    if data is None:
        return {}
    rows = [r for r in data["items"] if r.get("available") is True]
    pairs = collections.Counter((r.get("brand", "").strip().casefold(), r["name"]) for r in rows)
    keys = collections.Counter(_cache_key(r.get("brand", ""), r["name"]) for r in rows)
    out = {}
    for r in rows:
        brand, name = r.get("brand", ""), r["name"]
        pair, key = (brand.strip().casefold(), name), _cache_key(brand, name)
        if pairs[pair] > 1 or keys[key] > 1:
            continue
        thumbs = r.get("image_urls") or []
        out[pair] = AruDetail(
            id=str(r["id"]), name=name, brand=brand, url=r.get("url", ""),
            photo_url=original_photo_url(thumbs[0]) if thumbs else None,
            specs=r.get("specifications") or {},
            supplier_updated_at=data["generated_at"], cache_key=key)
    return out


def _is_photo(data) -> bool:
    return (isinstance(data, (bytes, bytearray)) and len(data) >= _MIN_PHOTO_BYTES
            and data[:2] == b"\xff\xd8")


def _get(fetch, url, retry_delay):
    for attempt in range(_ATTEMPTS):
        try:
            return fetch(url)
        except Exception:
            if attempt < _ATTEMPTS - 1:
                time.sleep(retry_delay)
    return None


def _download(detail: AruDetail, fetch: Callable[[str], bytes], retry_delay: float) -> bytes | None:
    """Оригинал по выведенному адресу, иначе og:image со страницы товара."""
    candidates = [detail.photo_url] if detail.photo_url else []
    for url in candidates + ["page"]:
        if url == "page":
            html = _get(fetch, detail.url, retry_delay) if detail.url else None
            m = _OG_RE.search(html.decode("utf-8", "replace")) if html else None
            if not m:
                continue
            url = m.group(1)
        data = _get(fetch, url, retry_delay)
        if _is_photo(data):
            return bytes(data)
    return None


def make_aru_prepare(store, details: dict, photos_dir, fetch: Callable[[str], bytes],
                     retry_delay: float = 1.0):
    """prepare(item) для tick: если позиция — товар АРУ с однозначными данными и в кэше
    ещё ничего нет, скачать фото и записать УТП+фото в research_cache. Любой сбой —
    молча оставить позицию обычному research."""
    photos = Path(photos_dir)

    def prepare(item) -> None:
        detail = details.get((item.brand.strip().casefold(), item.name))
        if detail is None:
            return
        key = f"{item.brand.strip().lower()}|{item.model.strip().lower()}"
        if detail.cache_key != key or store.cache_get(key):
            return
        utp = utp_from_specs(detail.specs)
        if not utp:
            return
        target = photos / f"{detail.id}.jpg"
        if not target.is_file():
            data = _download(detail, fetch, retry_delay)
            if data is None:
                return
            photos.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".jpg.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, target)
        store.cache_put(key, utp, str(target), source="aru", evidence={
            "source": "aru", "aru_id": detail.id, "source_url": detail.url,
            "supplier_updated_at": detail.supplier_updated_at})

    return prepare
