"""B2B-подпись для Telegram (как УТП-карточка stock_report_bot): шапка
«Бренд Наименование — цена · N шт.» + разделитель + блок «Ключевые особенности»
(мощность BTU·кВт·площадь + класс/компрессор/обогрев/шум/гарантия/Wi-Fi…).
Канал B2B — без розничного CTA. Категория-независимо: буллеты, которых нет в ТТХ,
просто не выводятся. Поддержан ручной override на серию (manifest)."""
from __future__ import annotations
import re
from content_factory.content.sizing import size_from_btu
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
        size = size_from_btu(f["btu"], f["category_id"])
        if size:
            parts.append(f"{size}000 BTU")
            if area is None and _AREA_BY_SIZE.get(size):
                area = _AREA_BY_SIZE[size]
    if area:
        parts.append(f"до {_trim(area)} м²")
    return " · ".join(parts)


def _header(f: dict, price) -> str:
    head = f"{f['brand']} {f['model_title']}".strip()
    tail = []
    if price:
        tail.append(_money(price))
    if f["qty"]:
        tail.append(f"{f['qty']} шт.")
    return head + (" — " + " · ".join(tail) if tail else "")


def render_caption(item, price, cfg, utp_raw=None) -> str:
    """B2B-подпись (≤ cfg.caption_max). `item` — Offer | SeriesGroup, `price` — int|None.
    cfg — ContentConfig (caption_max, stop_words, descriptions {series_key: ручной текст}).
    utp_raw — список преимуществ Бриза из API (для breeze; иначе берётся из ТТХ/«Описание»)."""
    f = _extract(item)
    cap_max = getattr(cfg, "caption_max", 1024)
    header = _header(f, price)

    override = (getattr(cfg, "descriptions", None) or {}).get(f["key"])
    if override:
        text = f"{header}\n{_DIVIDER}\n{override.strip()}"
    else:
        bullets = []
        power = _power_line(f)
        if power:
            bullets.append(f"❄️ {power}")
        bullets += build_specs_for_card(f["tech_rows"], f["brand"], f["series"], f["source"],
                                        utp_raw=utp_raw)
        lines = [header, _DIVIDER]
        if bullets:
            lines.append("Ключевые особенности:")
            lines += bullets
        text = "\n".join(lines)

    text = _strip_stopwords(text, getattr(cfg, "stop_words", [])).strip()
    if len(text) > cap_max:
        text = text[:cap_max].rstrip()
    return text
