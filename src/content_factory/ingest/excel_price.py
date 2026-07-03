"""Excel-прайс владельца (БытТехОпт и подобные): лист с колонками
«№ | Артикул | Бренд | Наименование | Цена | Заказ», разделы — строки, где заполнена
только первая ячейка. Источник «excel» контент-завода: выбор позиций под команду
/make N <категория> [бренд=K …] и ключи анти-дубля excel|бренд|модель."""
from __future__ import annotations
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PriceItem:
    section: str          # раздел прайса («Холодильники с нижней морозильной камерой»)
    article: str
    brand: str
    name: str             # полное наименование позиции
    price: int


_PAREN_RE = re.compile(r"\([^)]*\)")


def parse_price_xlsx(path) -> list[PriceItem]:
    """Позиции прайса (строки без цены/бренда пропускаются, разделы запоминаются)."""
    import openpyxl                                   # тяжёлый импорт — только по нужде
    wb = openpyxl.load_workbook(Path(path), read_only=True)
    ws = wb[wb.sheetnames[0]]
    items, section = [], ""
    for row in ws.iter_rows(values_only=True):
        a, art, brand, name, price = (list(row) + [None] * 5)[:5]
        if a and not art and not brand and not name:
            section = str(a).strip()                  # строка-раздел
            continue
        if not name or not brand:
            continue
        try:
            p = int(round(float(str(price).replace(" ", "").replace(",", "."))))
        except (TypeError, ValueError):
            continue                                  # без цены не публикуем
        if p <= 0:
            continue
        items.append(PriceItem(section=section, article=str(art or "").strip(),
                               brand=str(brand).strip(), name=str(name).strip(), price=p))
    wb.close()
    return items


def extract_model(name: str, brand: str) -> str:
    """Модель = часть наименования после бренда, без скобок:
    «Холодильник Stinol STS 167 (167*60*62)» → «STS 167»."""
    clean = _PAREN_RE.sub("", name or "").strip()
    m = re.search(re.escape(brand or ""), clean, re.IGNORECASE) if brand else None
    if m:
        tail = clean[m.end():].strip(" -–—·")
        if tail:
            return re.sub(r"\s+", " ", tail)
    return re.sub(r"\s+", " ", clean)


def item_key(item: PriceItem) -> str:
    """Ключ анти-дубля (как у пилота): excel|<бренд>|<модель> (lower)."""
    return f"excel|{item.brand.strip().lower()}|{extract_model(item.name, item.brand).lower()}"


def select_from_price(items: list[PriceItem], category_kw: str, quotas: dict,
                      count: int, taken: set) -> list[PriceItem]:
    """До `count` позиций категории (слово ищется в разделе И в наименовании).
    quotas: {'beko': 3, 'stinol': 2, '*': None} — сначала явные квоты брендов,
    затем добор любыми (если задан '*' или квот нет). Анти-дубль по taken-ключам."""
    kw = (category_kw or "").strip().lower()
    pool = [i for i in items
            if (kw in i.section.lower() or kw in i.name.lower()) and item_key(i) not in taken]
    out: list[PriceItem] = []

    explicit = {b: n for b, n in (quotas or {}).items() if b != "*" and n}
    for brand_kw, n in explicit.items():
        got = [i for i in pool if brand_kw in i.brand.lower() and i not in out][:n]
        out.extend(got)

    fill_any = "*" in (quotas or {}) or not explicit
    if fill_any:
        for i in pool:
            if len(out) >= count:
                break
            if i not in out:
                out.append(i)
    return out[:count]
