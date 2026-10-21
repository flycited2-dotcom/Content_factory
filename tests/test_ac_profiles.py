"""Профили кондиционеров из ТТХ поставщиков: мороз на обогрев, инвертор, тепловой насос, Full DC."""
import pytest

from content_factory.ingest.ac_profiles import build_profiles, frost_from_spec


@pytest.mark.parametrize("value, frost", [
    ("-15 ~ +24", 15), ("-25 ~ +32", 25), ("-20", 20), ("–15~30", 15), ("-30~24", 30), ("40", None), ("", None),
])
def test_frost_from_spec(value, frost):
    assert frost_from_spec(value) == frost


ROWS = [
    ("B1", "Инверторная сплит-система серии AKOYA RAC-I-AK25HP.", "Рабочие температурные границы наружного воздуха (нагрев)", "-15 ~ +24"),
    ("B1", "Инверторная сплит-система серии AKOYA RAC-I-AK25HP.", "Тепловой насос", "нет"),
    ("R1", "Сплит-система Ballu BSUI-09HN8", "Мин. рабочая температура воздуха для внешнего блока", "-25"),
    ("R1", "Сплит-система Ballu BSUI-09HN8", "Инверторная технология", "Да"),
    ("D1", "Daichi SIB25AVQS1R / SIB25FVS1R", "Диапазон рабочих температур, нагрев, °C", "-30~24"),
]


def test_build_profiles_matches_by_model_code_and_merges_text():
    items = [{"sku": "s1", "name": "Кондиционер FUNAI AKOYA RAC-I-AK25HP.D01", "group": "inv", "utp": ""},
             {"sku": "s2", "name": "Кондиционер BALLU BSUI-09HN8", "utp": "✓ Full DC инвертор"},
             {"sku": "s3", "name": "Кондиционер DAICHI SIBERIA SIB25AVQS1R", "utp": "✓ Тепловой насос до -30"},
             {"sku": "s4", "name": "Кондиционер без данных X-1000", "group": "onoff"}]
    p = build_profiles(ROWS, items)
    assert p["s1"] == {"min_temp": 15, "inverter": True, "heat_pump": False, "full_dc": False}
    assert p["s2"]["min_temp"] == 25 and p["s2"]["inverter"] is True and p["s2"]["full_dc"] is True
    assert p["s3"]["min_temp"] == 30 and p["s3"]["heat_pump"] is True
    assert p["s4"] == {"min_temp": None, "inverter": False, "heat_pump": False, "full_dc": False}
