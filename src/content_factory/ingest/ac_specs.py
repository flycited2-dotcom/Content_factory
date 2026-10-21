"""Характеристики кондиционера из названия и УТП каталога SplitHub — для отбора по словам владельца
(«9–12 BTU», «работают от −20»). Каталог отдельных полей не даёт: мощность зашита в модель-код
(MSAG3-09 — класс 09; AK25HP, RFS28HN — 2,5/2,8 кВт), морозостойкость — в УТП («Работа до -15 °С»).
Не распознали — None: такой товар под фильтр не попадает, а не угадывается.
"""
from __future__ import annotations

import re

SIZES = (7, 9, 12, 18, 24, 30, 36, 48, 60)
# класс kBTU прямо в коде: MSAG3-09N8, BSO-07HN8, YEL-09Z, ASX07H, AS-07UW4, inverter-9
_CLASS = re.compile(r"(?:(?<=[-_ ])|(?<=[A-Z]))(07|09|10|12|13|18|24|30|36|48|60)(?=[A-Z]|[-_/ ]|$)")
_CLASS_ALIAS = {10: 9, 13: 12}          # Hisense AS-10/AS-13 — те же типоразмеры 9 и 12
_CLASS_TAIL = re.compile(r"[-_ ](7|9|12|18|24)$")
# киловатты×10 в коде: AK25HP, RFS28HN, ARE22HN, FC55HN, FTXF20E, RAK-DJ18 (тут 18 — класс, ловится выше)
_KW = re.compile(r"(?<=[A-Z-])(20|21|22|25|26|27|28|32|33|34|35|36|40|42|45|50|52|53|55|60|61|62|63|65|68|70|71|72|75|80|85|90|100)(?=[A-Z.]|$)")
_KW_TO_CLASS = ((23, 7), (29, 9), (38, 12), (47, 15), (58, 18), (76, 24), (90, 30), (999, 36))
_TEMP = re.compile(r"до\s*[−–-]\s*(\d{1,2})\s*°")


def _code(name: str, sku: str) -> list[str]:
    """Кандидаты модель-кода: слова названия с цифрами (заглавными) и сам sku."""
    words = [w for w in re.split(r"\s+", name or "") if re.search(r"\d", w)]
    return [w.upper() for w in words] + [(sku or "").upper()]


def size_kbtu(name: str, sku: str = "") -> int | None:
    for code in _code(name, sku):
        m = _CLASS.search(code)
        if m:
            size = int(m.group(1))
            return _CLASS_ALIAS.get(size, size)
    for code in _code(name, sku):
        m = _KW.search(code)
        if m:
            kw10 = int(m.group(1))
            return next(size for limit, size in _KW_TO_CLASS if kw10 <= limit)
    m = _CLASS_TAIL.search((sku or "").upper())
    return int(m.group(1)) if m else None


def min_outdoor_temp(utp: str) -> int | None:
    """Нижняя граница работы на обогрев, градусы мороза: «Работа до -15 °С» → 15."""
    temps = [int(t) for t in _TEMP.findall(utp or "")]
    return max(temps) if temps else None
