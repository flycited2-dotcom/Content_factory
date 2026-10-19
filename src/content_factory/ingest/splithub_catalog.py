"""Источник «SplitHub»: живой каталог сайта splithub.ru (products.json — кондиционеры,
полупром, медная труба, расходка; остатки/цены обновляют парсеры сайта).

Раз в сутки (splithub_sync) каталог скачивается, фильтруется по наличию и ложится
снапшотом prices_dir/splithub.json. Дальше он выглядит для завода как ещё один
прайс-слот (load_price_slots): разделы = группы сайта становятся кнопками /task, а
дальше работает штатный конвейер. Фото и УТП берём с сайта — поэтому в research_cache
кладём их заранее, и тик пропускает дорогой research ChatGPT (сразу карточка)."""
from __future__ import annotations
import hashlib
import json
import os
import re
import time
from pathlib import Path

from content_factory.ingest.excel_price import PriceItem, extract_model, item_key

SLOT = "splithub"
SNAPSHOT_NAME = "splithub.json"
CATALOG_URL = "https://splithub.ru/products.json"
OVERRIDES_URL = "https://splithub.ru/api/admin.php?action=products_overrides_public"
PHOTO_BASE_URL = "https://splithub.ru/assets/img/products/"
MAX_UTP_LINES = 6

# группа сайта → (раздел-кнопка в /task, слово-категория в названии)
GROUPS = {
    "inv": ("Кондиционеры инверторные", "Кондиционер"),
    "onoff": ("Кондиционеры неинверторные (on/off)", "Кондиционер"),
    "pac_inv": ("Полупромышленные кондиционеры инверторные",
                "Полупромышленный кондиционер"),
    "pac_onoff": ("Полупромышленные кондиционеры on/off",
                  "Полупромышленный кондиционер"),
    "truba": ("Медная труба", ""),
    "rashod": ("Монтажные расходные материалы", ""),
}
_AC_GROUPS = ("inv", "onoff", "pac_inv", "pac_onoff")
# префикс кнопки «раздел по бренду» (callback/кнопка ≤32 символов — см. wizard_flow)
BRAND_PREFIX = {"inv": "Инвертор", "onoff": "On/Off", "pac_inv": "Полупром инв.",
                "pac_onoff": "Полупром on/off", "rashod": "Монтаж"}
_BUTTON_MAX = 32
_META_KEYS = {"sku", "active", "updated_at", "badge", "badge_label"}
_SUFFIX_RE = re.compile(r"\s+(inv|inverter|on/off)\s*$", re.IGNORECASE)


def _clean_model(p: dict) -> str:
    model = _SUFFIX_RE.sub("", str(p.get("model") or "").strip())
    brand = str(p.get("brand") or "").strip()
    if brand and model.lower().startswith(brand.lower() + " "):
        model = model[len(brand):].strip()
    return model


def product_brand(p: dict) -> str:
    """У трубы «бренд» на сайте — название категории; в карточке он не нужен."""
    return "" if p.get("group") == "truba" else str(p.get("brand") or "").strip()


def product_name(p: dict) -> str:
    """Название позиции для прайс-слота/карточки. Кондиционеры:
    «Кондиционер <бренд> <серия> <модель>» (серию не дублируем, если она уже в
    модели); труба/расходка — как на сайте."""
    group = p.get("group")
    kind = GROUPS.get(group, ("", ""))[1]
    if group not in _AC_GROUPS:
        return str(p.get("model") or "").strip()
    model = _clean_model(p)
    brand = product_brand(p)
    series = str(p.get("series") or "").strip()
    if series.lower().startswith(brand.lower() + " "):
        series = series[len(brand):].strip()
    parts = [kind, brand]
    if series and series.lower() not in model.lower():
        parts.append(series)
    parts.append(model)
    return " ".join(x for x in parts if x)


def available(p: dict, stock_ok) -> bool:
    try:
        price = int(p.get("price") or 0)
    except (TypeError, ValueError):
        return False
    return price > 0 and p.get("stock") in set(stock_ok)


def utp_text(p: dict) -> str:
    """УТП для карточки/подписи в формате research: «✓ пункт» построчно."""
    items = [str(x).strip() for x in (p.get("benefits") or []) if str(x).strip()]
    if not items:
        items = [x.strip() for x in str(p.get("cardBenef") or "").split("·") if x.strip()]
    return "\n".join(f"✓ {x}" for x in items[:MAX_UTP_LINES])


def brand_section(group: str, brand: str) -> str | None:
    """Раздел «группа · бренд» (кнопка в /task) — иначе в общем списке раздела
    (≤30 позиций) бренды из конца каталога, напр. Бирюса, вообще не видны."""
    prefix = BRAND_PREFIX.get(group)
    brand = (brand or "").strip()
    if not prefix or not brand:
        return None
    return f"{prefix} · {brand}"[:_BUTTON_MAX]


def apply_overrides(products: list, payload) -> list:
    """Наложить оверрайды сайта (api/admin.php?action=products_overrides_public):
    так витрина показывает цену/наличие/скрытие поверх products.json, а custom_products
    — позиции, добавленные в админке. active=0 — товар скрыт на сайте."""
    if not isinstance(payload, dict) or not payload.get("ok"):
        return products
    ov = payload.get("overrides") or {}
    out = []
    for p in products:
        o = ov.get(p.get("sku")) or ov.get(str(p.get("id")))
        if o:
            if str(o.get("active", 1)) == "0":
                continue
            p = {**p, **{k: v for k, v in o.items() if k not in _META_KEYS}}
        out.append(p)
    known = {p.get("sku") for p in out}
    for cp in payload.get("custom_products") or []:
        if str(cp.get("active", 1)) != "0" and cp.get("sku") not in known:
            out.append(cp)
    return out


def _interleave(entries: list[dict]) -> list[dict]:
    """В пределах группы бренды идут вперемешку (по кругу): первые 30 строк списка
    в /task покрывают разные бренды, а не один подряд."""
    by_group: dict = {}
    for e in entries:
        by_group.setdefault(e["group"], {}).setdefault(e["brand"], []).append(e)
    out = []
    for group, brands in by_group.items():
        queues = sorted(brands.values(), key=len, reverse=True)
        while any(queues):
            for q in queues:
                if q:
                    out.append(q.pop(0))
    return out


def _entry(p: dict) -> dict:
    group = p.get("group")
    return {"sku": str(p.get("sku") or p.get("id") or ""),
            "brand": product_brand(p), "name": product_name(p),
            "price": int(p["price"]), "group": group,
            "section": GROUPS[group][0],
            "sections": [x for x in (GROUPS[group][0], brand_section(group, product_brand(p)))
                         if x],
            "stock": p.get("stock"),
            "photo": str(p.get("photo") or ""), "utp": utp_text(p)}


def _price_item(e: dict, section: str | None = None) -> PriceItem:
    return PriceItem(section=section or e["section"], article=e["sku"], brand=e["brand"],
                     name=e["name"], price=int(e["price"]))


def build_snapshot(products, stock_ok, bad_photos=()) -> list[dict]:
    """Отобрать позиции для завода: известная группа, в наличии по политике,
    цена>0, есть реальное фото (заглушка «Фото готовится» из bad_photos не
    годится — из неё карточку не сделать). Анти-дубль по ключу завода."""
    out, seen = [], set()
    for p in products:
        if p.get("group") not in GROUPS or not available(p, stock_ok):
            continue
        photo = str(p.get("photo") or "")
        if not photo or photo in bad_photos:
            continue
        e = _entry(p)
        if not e["name"]:
            continue
        key = item_key(_price_item(e))
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return _interleave(out)


def snapshot_path(prices_dir) -> Path:
    return Path(prices_dir) / SNAPSHOT_NAME


def write_snapshot(prices_dir, snap: list[dict]) -> None:
    path = snapshot_path(prices_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"fetched_at": time.time(), "items": snap},
                              ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def read_snapshot(prices_dir) -> list[dict]:
    path = snapshot_path(prices_dir)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("items", [])


def load_items(prices_dir) -> list[PriceItem]:
    """По позиции на каждый её раздел (группа и «группа · бренд»): ключ один, поэтому
    /task не даст запустить одну модель дважды."""
    return [_price_item(e, sec) for e in read_snapshot(prices_dir)
            for sec in (e.get("sections") or [e["section"]])]


GROUP_BUTTONS = {
    "inv": "❄️ Инверторные сплит-системы",
    "onoff": "❄️ On/Off сплит-системы",
    "pac_inv": "🏢 Полупром инверторные",
    "pac_onoff": "🏢 Полупром On/Off",
    "truba": "🔧 Медная труба",
    "rashod": "🧰 Монтажные материалы",
}


def group_menu(prices_dir) -> list[tuple[str, str, int]]:
    """Кнопки первого уровня меню /task: [(группа, подпись, число позиций)] —
    только группы, в которых сейчас есть позиции (порядок как на сайте)."""
    counts: dict = {}
    for e in read_snapshot(prices_dir):
        counts[e["group"]] = counts.get(e["group"], 0) + 1
    return [(g, GROUP_BUTTONS[g], counts[g]) for g in GROUP_BUTTONS if counts.get(g)]


def brand_menu(prices_dir, group: str) -> list[tuple[str, str, int]]:
    """Второй уровень: [(раздел «группа · бренд», бренд, число позиций)], крупные
    бренды первыми. У группы без брендов (труба) — пусто: сразу список позиций."""
    counts: dict = {}
    for e in read_snapshot(prices_dir):
        if e["group"] == group and len(e.get("sections") or []) > 1:
            key = (e["sections"][1], e["brand"])
            counts[key] = counts.get(key, 0) + 1
    return [(sec, brand, n) for (sec, brand), n in
            sorted(counts.items(), key=lambda kv: (-kv[1], kv[0][1].lower()))]


def group_section(group: str) -> str:
    return GROUPS[group][0]


def card_mode() -> str:
    """Стиль карточек SplitHub-позиций (по умолчанию премиум «mcp», как у кондиционеров)."""
    return os.getenv("SPLITHUB_CARD_MODE", "mcp")


def card_mode_keys(prices_dir) -> set[str]:
    """Ключи позиций слота SplitHub — им ставится свой стиль карточки."""
    return {item_key(i) for i in load_items(prices_dir)}


def _model_key(e: dict) -> str:
    return f"{e['brand'].strip().lower()}|{extract_model(e['name'], e['brand']).strip().lower()}"


def _to_png(raw: bytes) -> bytes:
    """Фото сайта — webp, а агенту уходит как png: конвертируем, если есть Pillow."""
    try:
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.open(io.BytesIO(raw)).convert("RGB").save(buf, "PNG")
        return buf.getvalue()
    except Exception:
        return raw


def prefetch_photos(names, fetch_photo) -> dict[str, bytes | None]:
    """Скачать уникальные фото (раз за прогон). fetch_photo(имя) -> bytes|None."""
    return {n: fetch_photo(n) for n in sorted({x for x in names if x})}


def bad_photo_names(photos: dict, placeholder: bytes | None) -> set[str]:
    """Недоступные фото и заглушка «Фото готовится» (байт-в-байт, на сервере она
    разложена под десятки имён) — карточку из них не сделать."""
    return {n for n, raw in photos.items()
            if not raw or (placeholder and raw == placeholder)}


def seed_cache(store, snap: list[dict], photo_dir, photos: dict) -> int:
    """Положить УТП и фото сайта в research_cache — тик пойдёт сразу в карточку.
    photos — результат prefetch_photos. Файл перезаписывается только при смене
    содержимого. Ручные УТП владельца (source='manual') cache_put не затирает.
    Возвращает число засеянных позиций."""
    photo_dir = Path(photo_dir)
    photo_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for e in snap:
        raw = photos.get(e["photo"])
        if not raw:
            continue
        stem = Path(e["photo"]).stem
        target = photo_dir / f"{stem}.png"
        digest = hashlib.md5(raw).hexdigest()
        marker = photo_dir / f"{stem}.md5"
        if not target.exists() or not marker.exists() or marker.read_text() != digest:
            tmp = target.with_suffix(".tmp")
            tmp.write_bytes(_to_png(raw))
            os.replace(tmp, target)
            marker.write_text(digest)
        store.cache_put(_model_key(e), e["utp"], str(target.resolve()), source="splithub")
        n += 1
    return n


def cancel_gone(store, snap: list[dict], previously_listed) -> list[str]:
    """Позиции, пропавшие из наличия, но ещё не запущенные (status=new, в т.ч. по
    расписанию) — снимаем: не гоним в карточку то, чего уже нет. Уже запущенные
    не трогаем (владелец увидит превью и решит сам)."""
    now = {item_key(_price_item(e)) for e in snap}
    gone = []
    for key in sorted(set(previously_listed) - now):
        item = store.get(key)
        if item and item.status == "new":
            store.update(key, status="cancelled", error="нет в наличии на SplitHub")
            gone.append(key)
    return gone
