"""Краткое описание для Telegram-подписи (caption ≤ лимита).

В отличие от длинного avito-описания, здесь компактный пост: заголовок (бренд+серия+тип
с мощностью/площадью) + 1 строка пользы + цена + короткий призыв. Без внешних ссылок и
хэштегов (решение владельца). Текст детерминированно варьируется по артикулу; поддержан
ручной override на серию из manifest (как в avito-bridge), к нему дописывается живая цена."""
from __future__ import annotations
import hashlib
from content_factory.content.sizing import size_from_btu
from content_factory.catalog.series import series_key

# Тип по категории каталога (как в avito render).
_TYPE_LABEL = {2: "Настенная сплит-система", 6: "Полупромышленный кондиционер",
               7: "Мобильный кондиционер"}
# Рекомендованная площадь по типоразмеру (отраслевая таблица, kBTU → м²).
_AREA_BY_SIZE = {7: 20, 9: 25, 10: 28, 12: 35, 13: 38, 14: 40, 16: 45, 18: 50,
                 20: 55, 22: 60, 24: 70, 26: 75, 28: 80, 30: 85, 36: 100, 42: 120,
                 48: 140, 60: 170}

_BENEFITS = [
    "Быстрое охлаждение в жару и мягкий обогрев в межсезонье.",
    "Ровный комфортный микроклимат без сквозняков.",
]
_BENEFITS_INV = [
    "Инверторный компрессор: тихая работа и экономия электроэнергии.",
    "Инвертор плавно держит температуру — тихо и экономично.",
]
_BENEFITS_MOBILE = [
    "Мобильный формат без монтажа — готов к работе из коробки.",
    "Без установки: вывели воздуховод в окно — и готово.",
]
_CTA = [
    "Подберём модель под площадь и бюджет — напишите нам.",
    "Поможем с выбором и подскажем по доставке и монтажу — пишите.",
]


def _strip_stopwords(text: str, stop_words) -> str:
    out = text
    for w in (stop_words or []):
        out = out.replace(w, "").replace(w.capitalize(), "")
    return out


def _seed(sku: str) -> int:
    """Стабильное число из артикула — для детерминированной вариативности."""
    return int(hashlib.sha1((sku or "").encode("utf-8")).hexdigest(), 16)


def _pick(options: list[str], seed: int) -> str:
    return options[seed % len(options)]


def _money(p) -> str:
    return f"{int(p):,}".replace(",", " ") + " ₽"


def _is_inverter(text: str) -> bool:
    return "инвертор" in (text or "").lower()


def _extract(item) -> dict:
    """Нормализуем Offer | SeriesGroup в общий набор полей для подписи."""
    if hasattr(item, "representative"):                  # SeriesGroup
        rep = item.representative
        return dict(brand=item.brand, name=item.series, category_id=item.category_id,
                    btu=rep.btu_calc, sku=item.supplier_sku, key=getattr(item, "key", None))
    return dict(brand=item.brand, name=item.model, category_id=item.category_id,    # Offer
                btu=item.btu_calc, sku=item.supplier_sku, key=series_key(item))


def _headline(f: dict) -> str:
    type_label = _TYPE_LABEL.get(f["category_id"], "Кондиционер")
    name = f"{f['brand']} {f['name']}".strip()
    nl = name.lower()
    if f["category_id"] == 7:
        conveys = "мобильн" in nl or "кондиционер" in nl
    else:
        conveys = "сплит" in nl or "кондиционер" in nl
    lead = name if conveys else f"{type_label} {name}"   # не задваиваем тип, если он уже в названии
    size = size_from_btu(f["btu"], f["category_id"])
    if size:
        area = _AREA_BY_SIZE.get(size)
        tail = f"{size}000 BTU" + (f" · до {area} м²" if area else "")
        return f"{lead} — {tail}"
    return lead


def _benefit(f: dict, seed: int) -> str:
    if _is_inverter(f["name"]):
        return _pick(_BENEFITS_INV, seed)
    if f["category_id"] == 7:
        return _pick(_BENEFITS_MOBILE, seed)
    return _pick(_BENEFITS, seed)


def render_caption(item, price, cfg) -> str:
    """Подпись поста (≤ cfg.caption_max). `item` — Offer или SeriesGroup, `price` — int|None.
    cfg — ContentConfig (caption_max, stop_words, descriptions {series_key: ручной текст})."""
    f = _extract(item)
    seed = _seed(f["sku"])
    cap_max = getattr(cfg, "caption_max", 1024)
    price_line = f"Цена: {_money(price)}" if price else ""

    override = (getattr(cfg, "descriptions", None) or {}).get(f["key"])
    if override:
        body = override.strip()
        if price_line:
            room = cap_max - len(price_line) - 2          # резервируем место под цену
            if len(body) > room:
                body = body[:room].rstrip()
            text = f"{body}\n\n{price_line}"
        else:
            text = body[:cap_max]
    else:
        lines = [_headline(f), _benefit(f, seed)]
        if price_line:
            lines += ["", price_line]
        lines += [_pick(_CTA, seed)]
        text = "\n".join(lines)

    text = _strip_stopwords(text, getattr(cfg, "stop_words", [])).strip()
    if len(text) > cap_max:
        text = text[:cap_max].rstrip()
    return text
