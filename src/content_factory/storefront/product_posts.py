"""Товарные посты VK из каталога в наличии: продающий текст, фото, ссылка на карточку.

Текст собирается из данных самого каталога — названия, цены, описания и
характеристик производителя — без LLM и без выдумок: каждая цифра в посте
либо взята из карточки, либо проверена на живой странице сайта перед выбором.
Качество держится на шаблонах и отборе предложений, а не на генерации.
"""
from __future__ import annotations

import glob
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import httpx

# Группы товаров и то, какие названия категорий каталога в них попадают.
GROUP_BY_CATEGORY = (
    ("ac", ("сплит-систем", "кондиционер")),
    ("vent", ("вентиляцион", "рекуператор")),
    ("air", ("увлажнител", "осушител", "воздухоочист")),
    ("heater", ("конвектор", "инфракрасн", "тепловент", "тепловые завесы",
                "тепловые пушки", "масляные радиатор")),
    ("radiator", ("радиаторы отопления", "котл")),
    ("water", ("водонагревател", "бойлер")),
    ("floor", ("тёплые полы", "теплые полы", "тёплый пол", "теплый пол", "терморегулятор")),
)

# Вес групп по месяцам: осенью и зимой больше обогрева, весной и летом — кондиционеров.
_WARM = {"ac": 14, "vent": 10, "air": 10, "heater": 26, "radiator": 20, "water": 10, "floor": 10}
_COLD = {"ac": 25, "vent": 10, "air": 7, "heater": 24, "radiator": 14, "water": 10, "floor": 10}
_SPRING = {"ac": 38, "vent": 15, "air": 14, "heater": 6, "radiator": 8, "water": 11, "floor": 8}
_SUMMER = {"ac": 46, "vent": 12, "air": 14, "heater": 3, "radiator": 5, "water": 12, "floor": 8}
MONTH_WEIGHTS = {
    1: _COLD, 2: _COLD, 3: _SPRING, 4: _SPRING, 5: _SPRING, 6: _SUMMER,
    7: _SUMMER, 8: _SUMMER, 9: _WARM, 10: _WARM, 11: _WARM, 12: _COLD,
}

GOLDEN = 0.6180339887498949

_NUM_UNIT = re.compile(
    r"[0-9]+(?:[.,][0-9]+)?\s*(?:дБ|кВт|Вт|л\b|мл|м2|м²|м³|часов|час|°|%|секци|кг|лет|год|BTU)", re.I)
_BAD_SENTENCE = re.compile(
    r"https?://|www\.|®|™|патент|©|\.ru\b|youtube|гарантийн[а-я]* срок|"
    # Обрывки без подлежащего и общие слова о бренде: «Срок службы каждого…»,
    # «Все приборы FUNAI имеют реальные отличия…» (вышли на стену в ноябре 2026).
    r"каждого|каждой из|все приборы|реальные отличия|под заказ|уточняйте|split home|"
    r"дату выгрузки|подбер[её]м технику", re.I)
# Предложения про линейку, кабель и настройки — про серию или комплектацию, а не про выгоду.
# Предложение, которое опирается на предыдущую фразу, вырванное из описания читается обрывком.
_DEPENDENT_START = ("он ", "она ", "оно ", "они ", "его ", "её ", "их ", "такой ", "такая ",
                    "такие ", "данный ", "данная ", "также ", "практически ", "благодаря этому")
_SERIES_SENTENCE = re.compile(
    r"линейк|модельн|моделей|модели\b|серии\b|серия\b|типоразмер|кабел|докупа|комплектаци|"
    r"настроен|перезапуска|в комплект|2000|предназначен[а-я]* для работы|"
    r"бренд|компани|продукты |продукци|производств[а-я]* контрол|этапах производства|"
    r"условия эксплуатации|окружающего воздуха|относительной влажности|моделями|моделях|"
    # Диапазон «3-6 кВт», «20-70 м2», «от 6 500 до 24 000 BTU» — это про все модели линейки.
    r"[0-9]\s*[-–—]\s*[0-9][0-9 ]*\s*(?:кВт|Вт|м2|м²|л\b|BTU)|от [0-9][0-9 ]* до [0-9][0-9 ]*\s*(?:кВт|Вт|м2|м²|BTU)", re.I)

# Промышленные и нестандартные позиции — не для розничной группы.
_NOT_FOR_RETAIL_NAME = re.compile(
    r"канальн|кассетн|колонн|потолочн|антивандал|IP ?54|внутрипольн|промышл|ECO ?[0-9]{3}", re.I)
_NOT_FOR_RETAIL_CATEGORY = re.compile(r"полупромышленн|компактные моноблочные|мульти", re.I)
# Аксессуары и расходники: в «Кондиционерах» нагреватель дренажа за 593 ₽ — не витрина.
_ACCESSORY_NAME = re.compile(
    r"дренаж|кронштейн|пульт|сифон|трубк|крепеж|крепёж|фреон|хладагент|помп[аы]|насос|"
    r"защитн|чехол|рамк[аи]|решетк|решётк|заглушк|переходник|комплект монтажн", re.I)
PRICE_CAP = {"ac": 100_000, "vent": 150_000, "air": 40_000, "heater": 25_000,
             "radiator": 30_000, "water": 70_000, "floor": 40_000}
PRICE_FLOOR = {"ac": 8_000, "vent": 8_000, "air": 1_500, "heater": 1_200,
               "radiator": 2_500, "water": 1_500, "floor": 1_000}
_SKIP_ATTR = re.compile(r"упаковк|масса|гарантийн|сетевой кабель|модель|бренд|серия|ссылка|видео", re.I)

# Потребительские характеристики: (шаблон ключа, что писать, когда значение «да»).
BOOL_FEATURES = (
    (re.compile(r"тепловой насос", re.I), "Работает на обогрев"),
    (re.compile(r"инверторн", re.I), "Инверторная технология"),
    (re.compile(r"wi-?fi", re.I), "Управление со смартфона по Wi-Fi"),
    (re.compile(r"таймер", re.I), "Таймер отключения"),
    (re.compile(r"защита от перегрева", re.I), "Защита от перегрева"),
    (re.compile(r"ручка для перемещения", re.I), "Ручка для переноски"),
    (re.compile(r"самодиагностик", re.I), "Самодиагностика неисправностей"),
    (re.compile(r"встроенный гигростат", re.I), "Встроенный гигростат"),
    (re.compile(r"ночной режим", re.I), "Ночной режим"),
    (re.compile(r"пульт управления в комплекте", re.I), "Пульт в комплекте"),
)
VALUE_FEATURES = (
    (re.compile(r"минимальная температура обогрева", re.I), "Работает на обогрев до {v}"),
    (re.compile(r"гарантийный срок", re.I), "Гарантия {v}"),
    (re.compile(r"^хладагент$", re.I), "Хладагент {v}"),
    (re.compile(r"срок службы", re.I), "Срок службы {v}"),
    (re.compile(r"теплоотдача при \S?t ?70", re.I), "Теплоотдача {v}"),
    (re.compile(r"максимальное рабочее давление", re.I), "Рабочее давление до {v}"),
    (re.compile(r"макс\.? температура воды", re.I), "Нагрев воды до {v}"),
    (re.compile(r"макс\.? температура теплоносителя", re.I), "Теплоноситель до {v}"),
    (re.compile(r"макс\.? площадь обогрева", re.I), "Обогрев до {v}"),
    (re.compile(r"объем воды в радиаторе", re.I), "Объём воды в радиаторе {v}"),
    (re.compile(r"удельная мощность", re.I), "Мощность {v}"),
)

CITY = "в наличии в Симферополе"
PHONE = "+7 978 579-29-95"


@dataclass(frozen=True)
class CatalogItem:
    id: str
    url: str
    price: int
    group: str
    category: str
    picture: str
    name: str
    brand: str
    prose: str
    attrs: dict = field(default_factory=dict, compare=False)
    # Остальные фото карточки: первое бывает «белое на белом» или невыразительным.
    pictures: tuple = field(default=(), compare=False)


# Название товара надёжнее категории на сайте: в базе 112 радиаторов числятся
# «Бытовыми сплит-системами», 21 завеса — там же. Порядок важен: масляный радиатор —
# это обогреватель, а не радиатор отопления.
NAME_PREFIXES = (
    ("heater", ("радиатор масляный", "масляный радиатор", "конвектор", "обогреватель",
                "тепловая пушка", "пушка тепловая", "тепловентилятор", "завеса",
                "электрическая завеса", "тепловая завеса", "инфракрасн")),
    ("radiator", ("радиатор", "секция радиатора")),
    ("water", ("водонагреватель", "колонка", "бойлер")),
    ("air", ("увлажнитель", "осушитель", "очиститель воздуха", "воздухоочиститель")),
    ("floor", ("мат ", "мат нагревательный", "терморегулятор", "кабель нагревательный")),
    ("ac", ("сплит-система", "кондиционер", "мобильный кондиционер")),
    ("vent", ("приточ", "рекуператор", "бризер")),
)


def group_of_item(category: str, name: str) -> str:
    """Группа товара: по названию, а если оно ничего не говорит — по категории сайта."""
    low = name.strip().casefold()
    for group, prefixes in NAME_PREFIXES:
        if any(low.startswith(prefix) for prefix in prefixes):
            return group
    return group_of(category)


_HYDRONIC_SPEC = re.compile(r"теплоноситель|рабочее давление|теплоотдача при", re.I)


def refine_group(group: str, attrs: dict) -> str:
    """Конвектор с теплоносителем и рабочим давлением — водяной, часть системы отопления.

    Название и категория сайта называют его обогревателем, но «согреть одну комнату»
    он не может: ему нужна система отопления. Характеристики это выдают.
    """
    if group == "heater" and any(_HYDRONIC_SPEC.search(key) for key in attrs):
        return "radiator"
    return group


def group_of(category: str) -> str:
    low = category.casefold()
    for group, needles in GROUP_BY_CATEGORY:
        if any(needle in low for needle in needles):
            return group
    return "other"


def retail_ok(category: str, name: str, price: int) -> bool:
    """Годится ли позиция для розничной витрины: известная группа, не промышленная,
    не аксессуар и цена в пределах своей группы. Общая для всех источников каталога."""
    group = group_of_item(category, name)
    if group == "other" or price <= 0:
        return False
    if (_NOT_FOR_RETAIL_CATEGORY.search(category) or _NOT_FOR_RETAIL_NAME.search(name)
            or _ACCESSORY_NAME.search(name)):
        return False
    return PRICE_FLOOR[group] <= price <= PRICE_CAP[group]


def _brand(name: str) -> str:
    # Бренд в каталоге обычно идёт среди первых слов названия латиницей или капсом.
    words = name.split()
    for word in words[:4]:
        if word.isupper() or (word[:1].isupper() and word[1:].islower() and word.isascii()):
            return word.casefold()
    return words[0].casefold() if words else ""


def parse_description(desc: str) -> tuple[str, dict]:
    """Разделить описание на прозу производителя и словарь характеристик."""
    head, _, tail = desc.partition("Характеристики:")
    attrs = {}
    for match in re.finditer(r"^•\s*([^:\n]+):\s*(.+)$", tail, re.M):
        attrs[match.group(1).strip()] = match.group(2).strip()
    # В некоторых карточках характеристики лежат прямо в тексте через «;».
    for match in re.finditer(r"([А-ЯЁA-Z][^:;\n]{3,40}):\s*([^;\n]{1,40})(?:;|\.|$)", head):
        attrs.setdefault(match.group(1).strip(), match.group(2).strip())
    return head.strip(), attrs


def load_catalog(directory: str | Path) -> list[CatalogItem]:
    items = []
    for path in sorted(glob.glob(str(Path(directory) / "*.yml"))):
        root = ET.parse(path).getroot()
        categories = {c.get("id"): (c.text or "") for c in root.iter("category")}
        for offer in root.iter("offer"):
            def get(tag):
                return (offer.findtext(tag) or "").strip()
            try:
                price = int(float(get("price") or 0))
            except ValueError:
                continue
            category = categories.get(get("categoryId"), "")
            name = get("name")
            group = group_of_item(category, name)
            if not (get("picture") and get("url") and retail_ok(category, name, price)):
                continue
            prose, attrs = parse_description(get("description"))
            group = refine_group(group, attrs)
            items.append(CatalogItem(
                id=str(offer.get("id")), url=get("url"), price=price, group=group,
                category=category, picture=get("picture"), name=name,
                brand=_brand(name), prose=prose, attrs=attrs,
            ))
    return items


def money(value: int) -> str:
    return f"{int(value):,}".replace(",", " ") + " ₽"


# ── проза производителя → продающие предложения ─────────────────────────────

def benefit_sentences(item: CatalogItem, limit: int = 2) -> list[str]:
    title = item.name.casefold()
    scored = []
    seen = set()
    for line in item.prose.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if not line or line.casefold().startswith(title[:30]):
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            sentence = sentence.strip()
            if not 35 <= len(sentence) <= 190 or _BAD_SENTENCE.search(sentence) \
                    or _SERIES_SENTENCE.search(sentence):
                continue
            if not sentence[0].isupper() or sentence[-1] not in ".!?":
                continue
            if sentence.casefold().startswith(_DEPENDENT_START):
                continue
            if sentence.casefold() in seen or sentence.casefold().startswith(("описание", "характеристики")):
                continue
            seen.add(sentence.casefold())
            scored.append((bool(_NUM_UNIT.search(sentence)), len(scored), sentence))
    # Продаёт конкретика с цифрой и единицей. Общие слова («элегантный дизайн»)
    # берём лишь как единственное предложение, когда конкретики в описании нет.
    concrete = [row for row in scored if row[0]][:limit]
    chosen = concrete or [row for row in scored if not row[0]][:1]
    return [row[2] for row in sorted(chosen, key=lambda row: row[1])]


def _attr_bullets(item: CatalogItem, limit: int = 4) -> list[str]:
    ranked: list[tuple[int, str]] = []
    patterns = [p for p, _ in BOOL_FEATURES] + [p for p, _ in VALUE_FEATURES]
    for key, value in item.attrs.items():
        is_value_key = any(p.search(key) for p, _ in VALUE_FEATURES)
        if _SKIP_ATTR.search(key) and not is_value_key:
            continue
        low = value.casefold().strip(" .")
        for pattern, text in BOOL_FEATURES:
            if pattern.search(key) and low in {"да", "есть", "имеется"}:
                ranked.append((patterns.index(pattern), text))
        for pattern, template in VALUE_FEATURES:
            if pattern.search(key) and low not in {"нет", "-", ""}:
                ranked.append((patterns.index(pattern), template.format(v=value.strip())))
    # Порядок списка шаблонов — это порядок важности: обогрев и инвертор раньше гарантии.
    seen: dict[str, int] = {}
    for rank, text in sorted(ranked):
        seen.setdefault(text, rank)
    return list(seen)[:limit]


# ── характеристики поставщиков Бриз, Daichi, JAC ────────────────────────────
# Шаблоны выше писались под поля Русклимата. Когда владелец перестал с ним работать
# (11.2026), ни один товар остальных поставщиков не проходил порог, и лента опустела.

def _spec(item: CatalogItem, pattern: str) -> str:
    for key, value in item.attrs.items():
        if re.search(pattern, key, re.I):
            return str(value).strip()
    return ""


def _number(value: str) -> str:
    match = re.search(r"[0-9]+(?:[.,][0-9]+)?", value)
    return f"{float(match.group(0).replace(',', '.')):g}" if match else ""


def _heat_min(item: CatalogItem) -> str:
    """Нижняя граница работы на обогрев, если она ниже нуля: «−15»."""
    raw = (_spec(item, r"температурные границы наружного воздуха \(нагрев\)")
           or _spec(item, r"диапазон рабочих температур, нагрев")
           or _spec(item, r"нижний диапазон.*нагрев")
           or _spec(item, r"минимальная температура обогрева"))
    match = re.match(r"\s*[-−–—]\s*([0-9]{1,2})\b", raw)
    return f"−{match.group(1)}" if match else ""


def spec_facts(item: CatalogItem) -> dict:
    """Числа из таблицы характеристик кондиционера. Пустая строка — данных нет."""
    if item.group != "ac":
        return {}
    noise = _spec(item, r"минимальный уровень шума внутреннего блока")
    if not noise:
        levels = [float(v.replace(",", ".")) for v in re.findall(
            r"[0-9]+(?:[.,][0-9]+)?", _spec(item, r"уровень звукового давления, вб"))]
        noise = f"{min(levels):g}" if levels else ""
    technology = " ".join((_spec(item, r"^инверторная технология$"),
                           _spec(item, r"^технология работы$"),
                           _spec(item, r"тип управления компрессором"))).casefold().strip()
    wifi = _spec(item, r"wi-?fi").casefold()
    months = re.fullmatch(r"([0-9]+)\s*мес\.?", _spec(item, r"^срок гарантии$"))
    years = int(months.group(1)) // 12 if months and int(months.group(1)) % 12 == 0 else 0
    return {
        "area": _number(_spec(item, r"помещени[йя] площадью")),
        "cool_kw": _number(_spec(
            item, r"^холодопроизводительность(?: \(квт\)|, квт(?:, ном\.)?)?$")),
        "heat_min": _heat_min(item),
        "heat_kw": _number(_spec(
            item, r"^теплопроизводительность(?: \(квт\)|, квт(?:, ном\.)?)?$")),
        "pipe": _number(_spec(item, r"^максимальная длина (?:трассы|труб)")),
        "noise": _number(noise),
        "inverter": "да" if ("inverter" in technology or technology.startswith("да")) else "",
        "wifi": "да" if wifi == "да" or wifi.startswith("да, встроен") else "",
        "warranty": f"{years} {'года' if years in (2, 3, 4) else 'лет'}" if years > 1 else "",
    }


def spec_bullets(item: CatalogItem) -> list[str]:
    facts = spec_facts(item)
    if not facts:
        return []
    comma = {key: value.replace(".", ",") for key, value in facts.items()}
    lines = [
        facts["area"] and f"Для помещения до {comma['area']} м²",
        facts["cool_kw"] and f"Мощность охлаждения {comma['cool_kw']} кВт",
        facts["heat_min"] and f"Работает на обогрев до {facts['heat_min']} °C на улице",
        facts["noise"] and f"Шум от {comma['noise']} дБ",
        facts["inverter"] and "Инверторный компрессор",
        facts["wifi"] and "Управление со смартфона по Wi-Fi",
        facts["heat_kw"] and f"Мощность обогрева {comma['heat_kw']} кВт",
        facts["pipe"] and f"Трасса до {comma['pipe']} м",
        facts["warranty"] and f"Гарантия {facts['warranty']}",
    ]
    return [line for line in lines if line]


# У увлажнителей, обогревателей и водонагревателей Бриза таблицы характеристик нет,
# цифры есть только в описании. Берём их из предложений про саму модель (не про серию)
# и только рядом со словом, которое называет величину.
_PROSE_FACTS = (
    (r"(?:производительност\w*|интенсивност\w* увлажнения)[^.!?]{0,60}?([0-9]{2,4})\s*мл\s*/?\s*ч",
     "Увлажнение до {v} мл/ч", "{v} мл/ч", "увлажнение"),
    (r"осушени\w*[^.!?]{0,60}?([0-9]{1,3})\s*(?:л|литр\w*)\s*(?:/|в)\s*сутки",
     "Осушение {v} л в сутки", "{v} л/сутки", "осушение"),
    (r"площад\w*[^.!?]{0,30}?до\s+([0-9]{1,3})\s*(?:м2|м²|м\?|кв\.?\s?м|метров)",
     "Для помещения до {v} м²", "до {v} м²", "площадь помещения"),
    (r"мощност\w*[^.!?]{0,40}?([0-9]{3,4})\s*Вт", "Мощность {v} Вт", "{v} Вт", "мощность"),
    (r"(?:резервуар\w*|бак\w*)[^.!?]{0,50}?([0-9]{1,3}(?:[.,][0-9])?)\s*(?:л\b|литр)",
     "Бак {v} л", "{v} л", "объём бака"),
    (r"([0-9]{1,2})\s*час\w*[^.!?]{0,40}без долива|без долива[^.!?]{0,50}?([0-9]{1,2})\s*час",
     "До {v} часов без долива воды", "до {v} ч", "без долива воды"),
)


# Предложение про всю линейку: его цифры не относятся к конкретной модели.
_RANGE_SENTENCE = re.compile(
    r"линейк|модельн|моделей|моделями|моделях|представлен[аы]?\b|типоразмер|"
    r"[0-9]\s*(?:и|или|-|–|—)\s*[0-9][0-9 ]*\s*(?:кВт|Вт|м2|м²|л\b|литр|мл)|"
    r"от [0-9][0-9 ]* до [0-9]", re.I)


def prose_facts(item: CatalogItem) -> list[tuple[str, str, str]]:
    """Цифры из описания: (пункт поста, значение для плитки, подпись плитки)."""
    found: list[tuple[str, str, str]] = []
    # «25 м?.» — битый знак квадрата в базе сайта; точка после него не конец предложения.
    prose = re.sub(r"\s+", " ", item.prose).replace("м?.", "м².").replace("м?", "м²")
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", prose) if not _RANGE_SENTENCE.search(s)]
    for pattern, template, tile_value, tile_label in _PROSE_FACTS:
        for sentence in sentences:
            match = re.search(pattern, sentence, re.I)
            if match:
                value = next(g for g in match.groups() if g).replace(".", ",")
                found.append((template.format(v=value), tile_value.format(v=value), tile_label))
                break
    return found


def prose_bullets(item: CatalogItem) -> list[str]:
    return [bullet for bullet, _, _ in prose_facts(item)]


def feature_bullets(item: CatalogItem, limit: int = 5) -> list[str]:
    """Пункты поста: таблица характеристик кондиционера, иначе признаки и цифры описания."""
    specific = spec_bullets(item)
    if len(specific) >= 3:
        return specific[:limit]
    merged = list(dict.fromkeys(_attr_bullets(item) + specific + prose_bullets(item)))
    # «Работает на обогрев» рядом с «Работает на обогрев до −25 °C» — повтор.
    merged = [line for line in merged
              if not any(other != line and other.startswith(line) for other in merged)]
    return merged[:limit]


# ── числа для крючков ───────────────────────────────────────────────────────

def _first(pattern: str, *texts: str) -> str:
    for text in texts:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1).replace(",", ".")
    return ""


def _yes(item: CatalogItem, key_part: str) -> str:
    for key, value in item.attrs.items():
        if key_part in key.casefold() and value.casefold().strip(" .") in {"да", "есть", "имеется"}:
            return "да"
    return ""


def _single_model_area(item: CatalogItem) -> str:
    """Площадь только из предложения про эту модель: диапазон линейки — не её площадь."""
    if item.group == "ac":
        return ""
    for sentence in re.split(r"(?<=[.!?])\s+", item.prose):
        if _SERIES_SENTENCE.search(sentence):
            continue
        value = _first(r"до\s+([0-9]{1,3})\s*(?:м2|м²|кв\.?\s?м|квадратн)", sentence)
        if value:
            return value
    return ""


def facts_for_hook(item: CatalogItem) -> dict:
    blob = f"{item.name}\n{item.prose}"
    spec = spec_facts(item)
    cold = spec.get("heat_min", "")
    quiet = spec.get("noise", "")
    return {
        "area": spec.get("area", "").replace(".", ",") or _single_model_area(item),
        # Крючок про мороз — только для моделей, которые греют ниже −10 °C.
        "heat_min": cold if cold and int(cold[1:]) >= 10 else "",
        "quiet": quiet.replace(".", ",") if quiet and float(quiet) <= 24 else "",
        "heatpump": _yes(item, "тепловой насос"),
        "inverter": _yes(item, "инверторн"),
        "liters": _first(r"([0-9]{2,3})\s*л\b", item.name),
        "kw": _first(r"([0-9](?:[.,][0-9]+)?)\s*кВт", item.name, item.prose),
        "sections": _first(r"([0-9]{1,2})\s*секц", item.name),
        "sections_text": "",
        "noise": _first(r"от\s+([0-9]{2})\s*дБ", item.prose),
    }


HOOKS = {
    "ac": (
        ("heat_min", "За окном холодает, а этот кондиционер греет при морозе до {heat_min} °C. Есть в наличии."),
        ("heat_min", "Кондиционер не только на лето: эта модель работает на обогрев до {heat_min} °C на улице."),
        ("area", "Кондиционер для комнаты до {area} м². Есть в наличии, привезём и установим."),
        ("area", "Ищете кондиционер на {area} квадратов? Вот модель, которая есть в наличии."),
        ("quiet", "Тихий кондиционер для спальни: от {quiet} дБ на минимальной скорости. Есть в наличии."),
        ("heatpump", "Кондиционер, который не уходит в отпуск на зиму: у этой модели есть режим обогрева. Вот она в наличии."),
        ("heatpump", "Один прибор и на жару, и на осенние вечера: у этого кондиционера есть режим обогрева."),
        ("inverter", "Инверторный кондиционер плавно меняет обороты и держит температуру без рывков. Вот модель, которая есть в наличии."),
        ("inverter", "Хотите, чтобы температура не «пилила» вверх-вниз? Смотрите на инвертор. Вот модель в наличии."),
        ("", "Выбираете кондиционер? Вот модель в наличии, с честной ценой и монтажом по всему Крыму."),
        ("", "Кондиционер — это не роскошь, а комфорт на каждый день. Вот хороший вариант в наличии."),
    ),
    "vent": (
        ("area", "Душно, а форточку не открыть: шум и пыль с улицы. Приточная установка для помещения до {area} м² решает это без окна нараспашку."),
        ("", "Окна закрыты, а воздух свежий? Так работает приточная вентиляция. Вот модель в наличии."),
        ("", "Свежий воздух без сквозняков и уличного шума. Вот вентиляционная установка, которую можно привезти уже сейчас."),
    ),
    "air": (
        ("", "Сухой воздух зимой — это пересохшее горло и сон хуже обычного. Вот прибор, который это исправляет."),
        ("", "Влажность в доме — тихий фактор комфорта и здоровья. Присмотритесь к этому прибору, он в наличии."),
        ("", "Воздух в квартире бывает слишком сухим или слишком влажным. Вот аккуратный способ его выровнять."),
    ),
    "heater": (
        ("kw", "Холодные вечера не ждут. Обогреватель на {kw} кВт — тепло в комнате без ожидания отопления."),
        ("", "Холодно, а отопление ещё не включили? Вот обогреватель, который можно поставить сегодня."),
        ("", "Нужно быстро согреть комнату, гараж или дачу? Вот вариант в наличии, цена открытая."),
        ("", "Тепло там, где оно нужно: этот обогреватель включается и работает, без ремонта и монтажа."),
        ("", "Зябко по утрам и вечерам? Иногда проще согреть одну комнату, чем ждать тепла во всём доме."),
        ("", "Дача, гараж, мастерская или детская: вот обогреватель, который есть в наличии прямо сейчас."),
    ),
    "heater:curtain": (
        ("", "Каждый раз, когда открывается дверь, тепло уходит на улицу. Тепловая завеса закрывает проём воздушной стеной."),
        ("", "Входная группа магазина, склада или мастерской — самое холодное место. Вот тепловая завеса в наличии."),
    ),
    "heater:gun": (
        ("", "Быстро прогреть гараж, склад или стройку? Для этого и существует тепловая пушка. Вот модель в наличии."),
        ("", "Тепловая пушка — когда тепло нужно быстро и сразу. Вот вариант в наличии, цена открытая."),
    ),
    "radiator:convector": (
        ("", "Меняете отопление или достраиваете дом? Вот водяной конвектор, он есть в наличии."),
        ("", "Тепло от системы отопления, но в другом формате: водяной конвектор вместо привычной батареи."),
    ),
    "radiator": (
        ("sections", "Радиатор на {sections_text}: замена старой батареи без лишней возни. Модель в наличии."),
        ("", "Меняете отопление или достраиваете дом? Вот радиатор, который есть в наличии и не придётся ждать поставку."),
        ("", "Тёплая зима начинается с хорошего радиатора. Вот модель в наличии с ценой без сюрпризов."),
        ("", "Батарея — это на годы, поэтому выбирать её стоит спокойно. Вот модель, которую можно посмотреть и взять."),
        ("", "Холодная комната у окна? Возможно, пора менять радиатор. Вот вариант в наличии."),
    ),
    "water": (
        ("liters", "Водонагреватель на {liters} литров: горячая вода по расписанию семьи, а не по расписанию коммунальщиков."),
        ("", "Горячая вода без отключений и ожидания. Вот водонагреватель, который есть в наличии."),
        ("", "Горячая вода нужна всегда, а не по графику. Вот модель, которую можно взять уже на этой неделе."),
        ("", "Утренний душ не должен зависеть от графика отключений. Вот водонагреватель в наличии."),
    ),
    "floor": (
        ("", "Тёплый пол — это когда утром не хочется надевать носки. Вот комплект, который есть в наличии."),
        ("", "Тёплый пол решает вечную проблему холодного пола в ванной и на кухне. Вот вариант в наличии."),
    ),
}

_ASK = (
    "Подскажем, подойдёт ли вам именно эта модель, — напишите в сообщения сообщества.",
    "Есть вопросы по выбору? Ответим в сообщениях сообщества.",
)
# Монтаж упоминаем только там, где он есть: увлажнителю установка не нужна.
CTAS = {
    "ac": ("Привезём и установим по всему Крыму.", "Доставка и монтаж по Крыму, консультация бесплатная.") + _ASK,
    "vent": ("Привезём и установим по всему Крыму.", "Доставка и монтаж по Крыму, консультация бесплатная.") + _ASK,
    "radiator": ("Доставим по Крыму, поможем с подбором.",) + _ASK,
    "water": ("Доставим по Крыму.",) + _ASK,
    "floor": ("Доставим по Крыму и подскажем с монтажом.",) + _ASK,
    "heater": ("Доставим по Крыму.",) + _ASK,
    "air": ("Доставим по Крыму.",) + _ASK,
}


def _hook_key(item: CatalogItem) -> str:
    """Обогреватели бывают разные: у завесы и пушки свои сценарии, не «дача и гараж»."""
    low = item.category.casefold()
    if item.group == "heater" and "завес" in low:
        return "heater:curtain"
    if item.group == "heater" and "пушк" in low:
        return "heater:gun"
    if item.group == "radiator" and item.name.strip().casefold().startswith("конвектор"):
        return "radiator:convector"
    return item.group


def _sections_word(count: str) -> str:
    number = int(count)
    if 11 <= number % 100 <= 14:
        return "секций"
    return {1: "секция", 2: "секции", 3: "секции", 4: "секции"}.get(number % 10, "секций")


def _variant(item: CatalogItem, size: int, salt: str = "") -> int:
    return int(hashlib.sha1((item.id + salt).encode()).hexdigest(), 16) % size


def humanize(text: str) -> str:
    """Владелец просил тексты без длинных тире: они выдают машинный текст (15.11.2026)."""
    return text.replace("—", "-").replace("–", "-")


def write_post(item: CatalogItem, price: int | None = None, avoid: tuple[str, ...] = ()) -> str:
    """Собрать текст товарного поста; ссылка на карточку добавляется отдельно.

    avoid — первые строки недавних постов: один и тот же крючок пять раз подряд на
    стене выглядит как рассылка.
    """
    price = price or item.price
    facts = facts_for_hook(item)
    if facts["sections"]:
        facts["sections_text"] = f"{facts['sections']} {_sections_word(facts['sections'])}"
    hooks = HOOKS.get(_hook_key(item), HOOKS.get(item.group, ()))
    options = [text for need, text in hooks if not need or facts.get(need)]
    start = _variant(item, len(options), "hook") if options else 0
    rotated = [text.format(**facts) for text in options[start:] + options[:start]]
    seen = {humanize(line) for line in avoid}
    hook = next((text for text in rotated if humanize(text) not in seen),
                rotated[0] if rotated else "")
    parts = [hook, "", item.name, f"💎 {money(price)} · {CITY}"]
    sentences = benefit_sentences(item)
    if sentences:
        parts += ["", " ".join(sentences)]
    bullets = feature_bullets(item)
    # Один пункт выглядит как случайный огрызок: показываем список от двух.
    if len(bullets) >= 2:
        parts += [""] + [f"- {line}" for line in bullets]
    ctas = CTAS.get(item.group, _ASK)
    parts += ["", ctas[_variant(item, len(ctas), "cta")], f"📞 {PHONE}"]
    return humanize("\n".join(parts).strip())


def richness(item: CatalogItem) -> int:
    """Сколько содержательных фактов можно дать в посте: предложения с цифрой и
    пункты характеристик (список от двух пунктов считается за один факт, от четырёх — за два)."""
    concrete = sum(1 for sentence in benefit_sentences(item) if _NUM_UNIT.search(sentence))
    bullets = len(feature_bullets(item))
    return concrete + (1 if bullets >= 2 else 0) + (1 if bullets >= 4 else 0)


MIN_RICHNESS = 2


def is_postable(item: CatalogItem) -> bool:
    """Пост из одной фразы про линейку («завеса… мощностью 3–6 кВт») — не продажа: владелец
    получил такую карточку и забраковал. Нужны минимум два содержательных факта."""
    return richness(item) >= MIN_RICHNESS


# ── живая проверка карточки на сайте ────────────────────────────────────────

@dataclass(frozen=True)
class LiveInfo:
    price: int
    in_stock: bool


_LD = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)


def parse_live(html: str) -> LiveInfo | None:
    """Достать цену и наличие из разметки schema.org на странице товара."""
    for block in _LD.findall(html):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for node in (data if isinstance(data, list) else [data]):
            offers = node.get("offers") if isinstance(node, dict) else None
            if not offers:
                continue
            offers = offers[0] if isinstance(offers, list) else offers
            try:
                price = int(float(str(offers.get("price", "0"))))
            except ValueError:
                continue
            stock = "instock" in str(offers.get("availability", "")).casefold()
            return LiveInfo(price=price, in_stock=stock)
    # Запасной разбор для страниц, где разметка лежит не в offers.
    match = re.search(r'"price":\s*"([0-9]+)".{0,200}?"availability":\s*"([^"]+)"', html, re.S)
    if match:
        return LiveInfo(int(match.group(1)), "instock" in match.group(2).casefold())
    return None


def live_check(client: httpx.Client, item: CatalogItem) -> LiveInfo | None:
    try:
        response = client.get(item.url, headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    return parse_live(response.text)


# ── выбор: какая группа и какой товар в какой слот ──────────────────────────

def group_for_slot(ordinal: int, day: date) -> str:
    """Группа товара для слота. Последовательность золотого сечения ровно
    попадает в сезонные веса без случайности, поэтому календарь считается наперёд."""
    weights = MONTH_WEIGHTS[day.month]
    total = sum(weights.values())
    point = ((ordinal * GOLDEN) % 1.0) * total
    run = 0.0
    for group in sorted(weights):
        run += weights[group]
        if point < run:
            return group
    return sorted(weights)[-1]


def pick_item(items: list[CatalogItem], group: str, taken_ids: set[str],
              recent_brands: list[str], salt: str = "") -> CatalogItem | None:
    pool = sorted((i for i in items if i.group == group and i.id not in taken_ids
                   and is_postable(i)),
                  key=lambda i: hashlib.sha1((i.id + salt).encode()).hexdigest())
    if not pool:
        return None
    avoid = set(recent_brands[-2:])
    for item in pool:
        if item.brand not in avoid:
            return item
    return pool[0]


# ── фото: светлый фон и поля, чтобы товар выглядел как в карточке магазина ──

PHOTO_SIZE = 1080
# Чисто белый: у студийных снимков производителей фон белый, и любой другой тон
# проступает вокруг товара видимой рамкой.
PHOTO_BACKGROUND = (255, 255, 255)
MIN_SOURCE_SIDE = 280
MAX_UPSCALE = 3.0
# Ниже этой доли заметных пикселей снимок считается «белым на белом».
MIN_INK_SHARE = 0.04


def _ink_share(image) -> float:
    """Доля заметно непустых пикселей: у «белого на белом» она около нуля."""
    gray = image.convert("L").resize((200, 200))
    pixels = list(gray.getdata())
    return sum(1 for value in pixels if value < 235) / len(pixels)


def prepare_photo(client: httpx.Client, urls, destination: str | Path) -> Path | None:
    """Выбрать лучшее из фото производителя и положить на белый квадрат.

    Из нескольких снимков берётся самый выразительный (больше всего «чернил» на белом),
    затем кадр обрезается до содержимого, чтобы товар занимал рамку, а не терялся в
    пустом поле. None — ни один снимок не годится.
    """
    from io import BytesIO

    from PIL import Image

    candidates = [urls] if isinstance(urls, str) else [u for u in urls if u]
    best = None
    for url in dict.fromkeys(candidates):
        try:
            response = client.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=40,
                                  follow_redirects=True)
            response.raise_for_status()
            source = Image.open(BytesIO(response.content))
            source.load()
        except (httpx.HTTPError, OSError, ValueError):
            continue
        if min(source.size) < MIN_SOURCE_SIDE:
            continue
        rgba = source.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.split()[3])
        score = _ink_share(flat)
        if best is None or score > best[0]:
            best = (score, flat)
        if score >= MIN_INK_SHARE:
            # Фото производителя идут по убыванию «витринности»: первое достаточно
            # контрастное берём. Самый «чёрный» снимок побеждать нельзя: им бывает
            # чертёж с размерными стрелками, а не фото товара.
            best = (score, flat)
            break
    if best is None:
        return None
    flat = best[1]
    # Обрезка до содержимого: всё светлее 245 считаем фоном.
    box = flat.convert("L").point(lambda value: 255 if value < 245 else 0).getbbox()
    if box:
        margin = int(0.04 * max(flat.size))
        box = (max(0, box[0] - margin), max(0, box[1] - margin),
               min(flat.width, box[2] + margin), min(flat.height, box[3] + margin))
        flat = flat.crop(box)
    inner = int(PHOTO_SIZE * 0.84)
    # thumbnail только уменьшает, а обрезанный товар часто меньше рамки: растягиваем,
    # но не более чем втрое, чтобы не размыть исходный снимок.
    scale = min(inner / flat.width, inner / flat.height, MAX_UPSCALE)
    flat = flat.resize((max(1, round(flat.width * scale)), max(1, round(flat.height * scale))),
                       Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (PHOTO_SIZE, PHOTO_SIZE), PHOTO_BACKGROUND)
    canvas.paste(flat, ((PHOTO_SIZE - flat.width) // 2, (PHOTO_SIZE - flat.height) // 2))
    out = Path(destination)
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, "JPEG", quality=90)
    return out
