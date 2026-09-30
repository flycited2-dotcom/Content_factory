"""Серийная группировка позиций ПРАЙСА (запрос владельца 2026-07-07, кейс
EUROHOFF/Dantex): 3-5 мощностей одной серии → ОДИН пост (одна карточка, одни УТП,
цены линейкой), а не отдельный research на каждый типоразмер.

Аналог catalog/series.py для БД-источника, но у прайса нет поля series — размер
зашит в модель-код по-разному (AVE-07M, KSGHA21HFRN1, XG-TXE21RHA, TL-RWB100-NR).
Опора — явный токен «NN BTU» в наименованиях кондиционеров (есть у всех в
БытТехОпт): сигнатура серии = имя без BTU/площади, прогоны цифр ≥2 гасятся в «XX».
Одиночная цифра НЕ гасится — MSAG1/MSAG2/MSAG3 разные серии (поколения)."""
from __future__ import annotations
import re

_BTU = re.compile(r"(?<!\d)(\d{1,2})\s*BTU", re.I)
_AREA = re.compile(r"до\s+(\d+)\s*м²?", re.I)
_DIGRUN = re.compile(r"\d{2,}")


def parse_btu(name: str) -> tuple[int | None, int | None]:
    """(размер kBTU, площадь м²) из наименования; нет токена — None."""
    b = _BTU.search(name or "")
    a = _AREA.search(name or "")
    return (int(b.group(1)) if b else None), (int(a.group(1)) if a else None)


def series_sig(name: str) -> str | None:
    """Сигнатура серии (она же отображаемое имя поста): «DANTEX RK-XXSCDG On/Off
    (GREE)». None — в имени нет «NN BTU» (не кондиционер, серийность не про него)."""
    if not _BTU.search(name or ""):
        return None
    s = _BTU.sub(" ", name)
    s = _AREA.sub(" ", s)
    s = s.replace("·", " ").replace("•", " ")      # осиротевшие разделители тайла
    s = _DIGRUN.sub("XX", s)
    return re.sub(r"\s+", " ", s).strip(" -–,")


def group_rows(rows: list[tuple]) -> tuple[list[tuple], dict]:
    """Группировка перед постановкой в excel_items. rows — кортежи bot-путей
    (key, brand, model, name, price). Серия = один бренд + одна сигнатура, ≥2 членов:
    вместо них — ОДНА строка (ключ серии, представитель — младший размер, цена «от»).
    Остальные позиции проходят как есть, порядок сохранён.
    Возвращает (rows', members): members = {ключ серии: [{key, model, name, price,
    size, area}, …] по возрастанию размера} — для анти-дубля и линейки цен в превью."""
    sigs = [series_sig(r[3]) for r in rows]
    buckets: dict[tuple, list[int]] = {}
    for i, sig in enumerate(sigs):
        if sig:
            buckets.setdefault(((rows[i][1] or "").strip().lower(), sig.lower()),
                               []).append(i)
    out, members, done = [], {}, set()
    for i, r in enumerate(rows):
        sig = sigs[i]
        bkey = ((r[1] or "").strip().lower(), sig.lower()) if sig else None
        if bkey is None or len(buckets[bkey]) < 2:
            out.append(r)                          # не серия — как есть
            continue
        if bkey in done:
            continue                               # члены уже влиты в серию
        done.add(bkey)
        mlist = []
        for k, _b, m, n, p in (rows[j] for j in buckets[bkey]):
            size, area = parse_btu(n)
            mlist.append({"key": k, "model": m, "name": n, "price": p,
                          "size": size or 0, "area": area or 0})
        mlist.sort(key=lambda d: (d["size"], d["area"], d["name"]))
        brand = (r[1] or "").strip()
        skey = (f"excel|{brand.lower()}|{sig.lower()}" if brand
                else f"excel|{sig.lower()}")       # зеркально item_key; «xx» вместо
        rep = mlist[0]                             # цифр не столкнётся с позицией
        out.append((skey, brand, rep["model"], sig, min(d["price"] for d in mlist)))
        members[skey] = mlist
    return out, members
