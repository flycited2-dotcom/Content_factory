import pytest
from content_factory.ingest.title_parse import parse_model_title


@pytest.mark.parametrize("title,brand,series,kbtu", [
    ("Сплит-система Ballu Olympio Edge BSO-07HN8_22Y комплект", "Ballu", "Olympio Edge", 7),
    ("Сплит-система Ballu Olympio Edge BSO-24HN8_22Y комплект", "Ballu", "Olympio Edge", 24),
    ("Сплит-система инверторного типа Electrolux Smartline DC EACS/I-12HSM/N8_V2 комплект",
     "Electrolux", "Smartline DC", 12),
    ("Кондиционер мобильный Ballu Orbis BPAC-09 OR/N6", "Ballu", "Orbis", 9),
    ("Сплит-система инверторного типа Royal Thermo Diamond DC RTDI-09HN8/Wi-Fi комплект",
     "Royal Thermo", "Diamond DC", 9),
    ("Сплит-система AC ELECTRIC PRO ACEM-09HN8 комплект", "AC ELECTRIC", "PRO", 9),
    ("Сплит-система инверторного типа SHUFT Asgard DC Black SFTHAI-09HN8/BL комплект",
     "SHUFT", "Asgard DC Black", 9),
    # нет имени серии → префикс модель-кода
    ("Сплит-система инверторного типа Ballu BSPI-10HN8/BL/EU комплект", "Ballu", "BSPI", 10),
])
def test_parse(title, brand, series, kbtu):
    s, k = parse_model_title(title, brand)
    assert (s, k) == (series, kbtu)


def test_no_code_returns_none():
    assert parse_model_title("Труборез Ballu Super Stars ST-670", "Ballu") == ("Super Stars", None) \
        or parse_model_title("Средство моющее Ballu", "Ballu") == (None, None)


def test_groups_same_series_across_sizes():
    # 07 и 24 одной серии → одинаковое имя серии (группировка схлопнёт в одно объявление)
    s7, _ = parse_model_title("Сплит-система Ballu Olympio Edge BSO-07HN8_22Y комплект", "Ballu")
    s24, _ = parse_model_title("Сплит-система Ballu Olympio Edge BSO-24HN8_22Y комплект", "Ballu")
    assert s7 == s24 == "Olympio Edge"
