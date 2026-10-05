"""B2B-подпись для Telegram (как УТП-карточка stock_report_bot): шапка
«Бренд Наименование — цена · N шт.» + разделитель + блок «Ключевые особенности»
(мощность BTU·кВт·площадь + класс/компрессор/обогрев/шум/гарантия/Wi-Fi…).
Канал B2B — без розничного CTA. Категория-независимо: буллеты, которых нет в ТТХ,
просто не выводятся. Поддержан ручной override на серию (manifest)."""
from __future__ import annotations
import re
from content_factory.content.sizing import size_for
from content_factory.catalog.series import series_key
from content_factory.content.specs import build_specs_for_card

# Точные ТТХ мощности/площади (точнее, чем btu_calc).
_SPEC_KBTU = "Холодопроизводительность (kBTU)"
_SPEC_KW = "Холодопроизводительность (кВт)"
_SPEC_AREA = "Эффективен для помещений площадью до"
_NUM_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?")
_AREA_BY_SIZE = {7: 20, 9: 25, 10: 28, 12: 35, 13: 38, 14: 40, 16: 45, 18: 50,
                 20: 55, 22: 60, 24: 70, 26: 75, 28: 80, 30: 85, 36: 100, 42: 120,
                 48: 140, 60: 170}
_DIVIDER = "═" * 26
# Тип техники в начале наименования: «Настенная сплит-система …», «Мобильный
# кондиционер …». Не более трёх слов, чтобы не утащить в заголовок пол-названия.
_TYPE_PREFIX_RE = re.compile(r"^([А-ЯЁ][а-яё]+(?:[- ][а-яё]+){0,2})")


def _strip_stopwords(text: str, stop_words) -> str:
    out = text
    for w in (stop_words or []):
        out = out.replace(w, "").replace(w.capitalize(), "")
    return out


def _money(p) -> str:
    return f"{int(p):,}".replace(",", " ") + " ₽"


def _num(s):
    m = _NUM_RE.search(str(s if s is not None else ""))
    return float(m.group(0).replace(",", ".")) if m else None


def _trim(x: float) -> str:
    return f"{x:g}"


def _tech_rows(attrs_list) -> list[dict]:
    """Список словарей attrs → плоский список {title,value} для specs.py."""
    rows = []
    for attrs in attrs_list:
        for t, v in (attrs or {}).items():
            rows.append({"title": t, "value": v})
    return rows


def _storefront_features(f: dict) -> list[str]:
    """Проверяемые B2C-характеристики стабилизатора из названия/API без домыслов."""
    title = f.get("model_title") or ""
    attrs = f.get("attrs") or {}
    lines: list[str] = []
    patterns = [
        (r"(?<!\d)(\d[\d\s]*(?:[.,]\d+)?)\s*(кВА|ВА)(?![А-Яа-я])", "⚡ Мощность: {} {}"),
        (r"(?<!\d)(\d[\d\s]*(?:[.,]\d+)?)\s*(кВт|Вт)(?![А-Яа-я])", "🔌 Активная мощность: {} {}"),
        (r"(?<!\d)(\d+(?:[.,]\d+)?)\s*А(?![А-Яа-я])", "🔋 Ток: {} А"),
        (r"КПД\s*(\d+(?:[.,]\d+)?)\s*%", "📈 КПД: {}%"),
    ]
    for pattern, template in patterns:
        match = re.search(pattern, title, re.I)
        if match:
            values = [x.replace(" ", "") for x in match.groups() if x is not None]
            lines.append(template.format(*values))
    if re.search(r"циф\.?\s*индикац", title, re.I):
        lines.append("🖥 Цифровая индикация напряжения")
    voltage_lines: list[str] = []
    title_range = re.search(
        r"(?:вход\w*|вх\.?|рабоч\w*\s+диапазон|диапазон\s+вход\w*)"
        r"[^0-9]{0,30}(\d{2,3})\s*[-–—]\s*(\d{2,3})\s*В",
        title,
        re.I,
    )
    if title_range:
        voltage_lines.append(
            f"🔌 Диапазон входного напряжения: {title_range.group(1)}–{title_range.group(2)} В"
        )
    for key, value in attrs.items():
        key_lower = str(key).lower()
        if ("напряж" not in key_lower or "диапазон" not in key_lower
                or "выход" in key_lower or "output" in key_lower):
            continue
        match = re.search(r"(\d{2,3})\s*[-–—]\s*(\d{2,3})\s*В?", str(value), re.I)
        if not match:
            continue
        label = "Расширенный диапазон входного напряжения" if any(
            marker in key_lower for marker in ("расшир", "предель")
        ) else "Рабочий диапазон входного напряжения"
        voltage_lines.append(f"🔌 {label}: {match.group(1)}–{match.group(2)} В")
    lines.extend(dict.fromkeys(voltage_lines))
    warranty = next((str(v).strip() for k, v in attrs.items()
                     if "гарант" in k.lower() and str(v).strip() not in {"", "0", "0.0"}), "")
    if warranty:
        if warranty.isdigit():
            warranty += " мес."
        lines.append(f"🛡 Гарантия: {warranty}")
    delivery = attrs.get("Срок поставки, дней")
    if delivery and str(delivery).strip() not in {"0", "0.0"}:
        lines.append(f"🚚 Ориентировочный срок поставки: {delivery} дн.")
    return list(dict.fromkeys(lines))[:8]


def _render_storefront_caption(f: dict, price, cap_max: int) -> str:
    # Название API уже содержит бренд; не дублируем его отдельным префиксом.
    header_fields = dict(f)
    header_fields["brand"] = ""
    lines = [_header(header_fields, price), _DIVIDER]
    features = _storefront_features(f)
    if features:
        lines += ["Основные характеристики:", *features, ""]
    lines += ["Подберём модель под нагрузку и параметры вашей сети.",
              "Заказ и консультация: Крым, Запорожская и Херсонская области."]
    return "\n".join(lines).strip()[:cap_max].rstrip()


def _extract(item) -> dict:
    """Нормализуем Offer | SeriesGroup в набор полей для B2B-подписи."""
    if hasattr(item, "representative"):                  # SeriesGroup
        rep = item.representative
        members = list(item.members)
        return dict(brand=item.brand, series=item.series, category_id=item.category_id,
                    btu=rep.btu_calc, key=getattr(item, "key", None), source=item.source,
                    model_title=rep.model, qty=(rep.stock or 0), attrs=rep.attrs or {},
                    tech_rows=_tech_rows([m.attrs for m in members]),
                    titles=[m.model for m in members])
    return dict(brand=item.brand, series=item.series or item.model,            # Offer
                category_id=item.category_id, btu=item.btu_calc, key=series_key(item),
                source=item.source, model_title=item.model, qty=(item.stock or 0),
                attrs=item.attrs or {}, tech_rows=_tech_rows([item.attrs]),
                titles=[item.model])


def _power_line(f: dict) -> str:
    """Мощность/площадь из реальных ТТХ (BTU · кВт · площадь). btu_calc/таблица — fallback."""
    a = f.get("attrs") or {}
    kbtu = _num(a.get(_SPEC_KBTU))
    kw = _num(a.get(_SPEC_KW))
    area = _num(a.get(_SPEC_AREA))
    parts: list[str] = []
    if kbtu:
        parts.append(f"{int(round(kbtu * 1000))} BTU")
    if kw:
        parts.append(f"{_trim(kw)} кВт")
    if not parts:
        size = size_for(f["source"], f["model_title"], f["btu"], f["category_id"])
        if size:
            parts.append(f"{size}000 BTU")
            if area is None and _AREA_BY_SIZE.get(size):
                area = _AREA_BY_SIZE[size]
    if area:
        parts.append(f"до {_trim(area)} м²")
    return " · ".join(parts)


def _header(f: dict, price) -> str:
    """Название — первой строкой; цена (+остаток) — отдельной плашкой-цитатой
    (blockquote) с 💎 и жирным номиналом (выбор владельца 2026-07-05: цена
    выделена как отдельная цитата, номинал жирный)."""
    head = f"{f['brand']} {f['model_title']}".strip()
    if price:
        tail = [f"<b>{_money(price)}</b>"]
        if f["qty"] and f.get("source") != "storefront":
            tail.append(f"{f['qty']} шт.")
        return head + "\n<blockquote>💎 " + " · ".join(tail) + "</blockquote>"
    if f["qty"] and f.get("source") != "storefront":
        return f"{head} — {f['qty']} шт."
    return head


# ── серийный формат (выбор владельца 2026-07-03): пост продаёт линейку целиком ─
def _series_header(f: dict, in_stock) -> str:
    """Заголовок без артикула конкретной модели: «Бренд Тип серии Серия — от X ₽».
    Тип берём из наименования до слова «серии» («Инверторная сплит-система…»)."""
    mt = f.get("model_title") or ""
    if " серии " in mt:
        head = f"{f['brand']} {mt.split(' серии ')[0].strip()} серии {f['series']}".strip()
    else:
        # Без слова «серии» заголовок схлопывался до «Бренд + код серии»: живой
        # пост назывался «Axioma H», хотя тип техники был в наименовании.
        kind = _TYPE_PREFIX_RE.match(mt.strip())
        head = (f"{f['brand']} {kind.group(1)} серии {f['series']}".strip()
                if kind and f["series"] else f"{f['brand']} {f['series']}".strip())
    prices = [p for _, p in in_stock if p]
    if prices:
        head += f"\n<blockquote>💎 <b>от {_money(min(prices))}</b></blockquote>"
    return head


def _series_lines(in_stock) -> list[str]:
    """Строки линейки: «▫️ 07 · 22 390 ₽ · 17 шт.» (мощность · цена · остаток)."""
    out = []
    def _sz(m):
        return size_for(m.source, m.model, m.btu_calc, m.category_id)

    # сортируем по исправленному размеру (btu_calc в БД местами хаотичен)
    for m, p in sorted(in_stock, key=lambda t: (_sz(t[0]) or 0, t[0].btu_calc or 0)):
        size = _sz(m)
        bits = [f"{size:02d}" if size else (m.model or "?")[:24]]
        if p:
            bits.append(_money(p))
        if m.stock and m.source != "storefront":
            bits.append(f"{m.stock} шт.")
        out.append("▫️ " + " · ".join(bits))
    return out


def render_caption(item, price, cfg, utp_raw=None, member_prices=None) -> str:
    """B2B-подпись (≤ cfg.caption_max). `item` — Offer | SeriesGroup, `price` — int|None.
    cfg — ContentConfig (caption_max, stop_words, descriptions {series_key: ручной текст}).
    utp_raw — список преимуществ Бриза из API (для breeze; иначе берётся из ТТХ/«Описание»).
    member_prices — [(offer, цена|None)] членов серии: при ≥2 в наличии подпись становится
    серийной (заголовок «от X ₽» без артикула + строки мощность/цена/остаток)."""
    f = _extract(item)
    cap_max = getattr(cfg, "caption_max", 1024)
    if f.get("source") == "storefront":
        text = _render_storefront_caption(f, price, cap_max)
        return _strip_stopwords(text, getattr(cfg, "stop_words", [])).strip()
    in_stock = [(m, p) for m, p in (member_prices or []) if (m.stock or 0) > 0]
    serial = len(in_stock) >= 2
    header = _series_header(f, in_stock) if serial else _header(f, price)

    # Ручное описание заменяет собой буллеты ТТХ, но не живые цены: наличие и
    # цена сверяются перед публикацией и не могут быть вытеснены готовым текстом.
    override = (getattr(cfg, "descriptions", None) or {}).get(f["key"])
    lines = [header, _DIVIDER]
    if override:
        lines += [override.strip(), ""]
    if serial:
        lines.append("Модели и цены:")
        lines += _series_lines(in_stock)
        lines.append("")
    if not override:
        bullets = []
        power = _power_line(f)
        if power:
            bullets.append(f"❄️ {power}")
        bullets += build_specs_for_card(f["tech_rows"], f["brand"], f["series"], f["source"],
                                        utp_raw=utp_raw)
        if bullets:
            lines.append("Ключевые особенности:")
            lines += bullets
    text = "\n".join(lines).rstrip()

    text = _strip_stopwords(text, getattr(cfg, "stop_words", [])).strip()
    if len(text) > cap_max:
        text = text[:cap_max].rstrip()
    return text
