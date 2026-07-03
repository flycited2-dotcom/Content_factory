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


def _price_int(val) -> int | None:
    """'37 520,00 RUB' / '40451' / 40451.0 → int (иначе None)."""
    digits = re.sub(r"[^0-9,.]", "", str(val if val is not None else ""))
    if not digits:
        return None
    try:
        p = int(round(float(digits.replace(" ", "").replace(",", "."))))
    except ValueError:
        return None
    return p if p > 0 else None


def _parse_table(ws) -> list[PriceItem]:
    """Формат-таблица (БытТехОпт): № | Артикул | Бренд | Наименование | Цена."""
    items, section = [], ""
    for row in ws.iter_rows(values_only=True):
        a, art, brand, name, price = (list(row) + [None] * 5)[:5]
        if a and not art and not brand and not name:
            section = str(a).strip()                  # строка-раздел
            continue
        if not name or not brand:
            continue
        p = _price_int(price)
        if p is None:
            continue                                  # без цены не публикуем
        items.append(PriceItem(section=section, article=str(art or "").strip(),
                               brand=str(brand).strip(), name=str(name).strip(), price=p))
    return items


_1C_CODE_RE = re.compile(r"^(УТ-|00-)\S+")


def _parse_1c_blocks(ws) -> list[PriceItem]:
    """Формат «1С-карточки» (прайсы вида ИП Аксёнов): товар — блок строк
    (название → [характеристики] → «Код|Артикул|Цена» → значения «…RUB»),
    разделы — одиночный текст в первых колонках, колонки бренда нет."""
    items, section, name = [], "", None
    prev_blank = True
    for row in ws.iter_rows(values_only=True):
        cells = {j: str(c).strip() for j, c in enumerate(row)
                 if c is not None and str(c).strip()}
        texts = [t for t in cells.values() if "ФОТОГРАФИИ" not in t]
        if not texts:
            prev_blank = True
            continue
        if set(texts) >= {"Код", "Цена"}:             # строка-шапка блока
            prev_blank = False
            continue
        if _1C_CODE_RE.match(texts[0]):               # строка значений: код, артикул, цена
            price = next((_price_int(t) for t in texts if "RUB" in t or "руб" in t.lower()),
                         None) or (_price_int(texts[-1]) if len(texts) >= 2 else None)
            art = texts[1] if len(texts) >= 3 else ""
            if name and price:
                items.append(PriceItem(section=section, article=art, brand="",
                                       name=name, price=price))
            name, prev_blank = None, False
            continue
        low_keys = [j for j in cells if j <= 2]
        if low_keys and len(texts) == 1 and cells.get(low_keys[0]) == texts[0]:
            section = texts[0]                        # раздел/подраздел
            prev_blank = True
            continue
        if prev_blank and texts:                      # первая строка блока = название
            name = re.sub(r"\s+", " ", texts[0])
        prev_blank = False
    return items


def parse_price_xlsx(path) -> list[PriceItem]:
    """Позиции прайса. Стратегии: таблица (БытТехОпт) → блоки 1С (ИП Аксёнов и т.п.)."""
    import openpyxl                                   # тяжёлый импорт — только по нужде
    wb = openpyxl.load_workbook(Path(path), read_only=True)
    items: list[PriceItem] = []
    for sheet in wb.sheetnames:
        ws = wb[sheet]
        got = _parse_table(ws)
        if not got:
            got = _parse_1c_blocks(ws)
        items.extend(got)
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
    """Ключ анти-дубля (как у пилота): excel|<бренд>|<модель> (lower).
    Прайсы без колонки бренда → excel|<наименование без скобок> (lower)."""
    if item.brand.strip():
        return (f"excel|{item.brand.strip().lower()}"
                f"|{extract_model(item.name, item.brand).lower()}")
    clean = re.sub(r"\s+", " ", _PAREN_RE.sub("", item.name)).strip().lower()
    return f"excel|{clean}"


def select_from_price(items: list[PriceItem], category_kw: str, quotas: dict,
                      count: int, taken: set) -> list[PriceItem]:
    """До `count` позиций категории (слово ищется в разделе И в наименовании).
    quotas: {'beko': 3, 'stinol': 2, '*': None} — сначала явные квоты брендов,
    затем добор любыми (если задан '*' или квот нет). Анти-дубль по taken-ключам."""
    kw = (category_kw or "").strip().lower()
    pool = [i for i in items
            if (kw in i.section.lower() or kw in i.name.lower()) and item_key(i) not in taken]
    # слово в НАЗВАНИИ товара важнее, чем в разделе («генератор» ≠ АВР из того же раздела)
    pool.sort(key=lambda i: 0 if kw in i.name.lower() else 1)
    out: list[PriceItem] = []

    explicit = {b: n for b, n in (quotas or {}).items() if b != "*" and n}
    for brand_kw, n in explicit.items():
        got = [i for i in pool
               if brand_kw in f"{i.brand} {i.name}".lower() and i not in out][:n]
        out.extend(got)

    fill_any = "*" in (quotas or {}) or not explicit
    if fill_any:
        for i in pool:
            if len(out) >= count:
                break
            if i not in out:
                out.append(i)
    return out[:count]
