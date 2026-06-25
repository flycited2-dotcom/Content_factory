"""Извлечение серии и типоразмера из НАЗВАНИЯ товара — для источников без поля `series`
(rusklimat: серия пустая, а btu_calc недостоверен — у всех размеров серии стоит одно число).
Серия и мощность зашиты в модель-коде: «Ballu Olympio Edge BSO-07HN8 комплект» → серия
'Olympio Edge', размер 7 (07). См. §P1 (группировка в серии)."""
from __future__ import annotations
import re

# Префиксы-типы (длинные раньше коротких — чтобы срезать максимально полный).
_PREFIXES = [
    "сплит-система инверторного типа",
    "сплит-система кассетного типа",
    "сплит-система напольно-потолочного типа",
    "сплит-система",
    "кондиционер мобильный",
    "мобильный кондиционер",
    "комплект",
]
# Модель-код: 2+ заглавных (возм. через / или _: EACS/I, BLCI_CF) + дефис + 2 цифры мощности.
_CODE = re.compile(r"\b([A-Z]{2,}(?:[/_][A-Z]+)*)-(\d{2})")


def parse_model_title(title: str | None, brand: str | None) -> tuple[str | None, int | None]:
    """(серия, размер kBTU) из названия. Любой элемент может быть None, если не распознан."""
    t = (title or "").strip()
    low = t.lower()
    for p in _PREFIXES:                       # срезать префикс-тип
        if low.startswith(p):
            t = t[len(p):].strip()
            break
    b = (brand or "").strip()                 # срезать бренд
    if b and t.lower().startswith(b.lower()):
        t = t[len(b):].strip()
    m = _CODE.search(t)
    if not m:
        return None, None
    series = t[:m.start()].strip(" -–·,/")
    if not series:                            # нет имени серии → префикс модель-кода (BSPI, BLCI_CF)
        series = m.group(1)
    return (series or None), int(m.group(2))
