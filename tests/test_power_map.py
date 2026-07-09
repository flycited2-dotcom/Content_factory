from content_factory.content.sizing import (
    load_power_map, power_codes, set_power_map, size_for)


def setup_function():
    set_power_map({})                       # чистый реестр перед каждым тестом


def test_power_codes_from_model_text():
    # код прилип к буквенному модельному коду; кириллица/слова без цифр не дают кодов
    assert power_codes("Инверторная сплит-система серии CITY AS-07UW4RYRKB01") == {"07"}
    assert power_codes("Классическая сплит-система серии SENSEI RAC-SN20HP.D07") == {"20", "07"}


def test_refrigerant_not_a_code():
    # R32/R410A — хладагент, не мощность
    assert "32" not in power_codes("Axioma ASX07H1Z1R/ASB07H1Z1R Серия H Инвертор R32")
    assert "41" not in power_codes("Сплит R410A KSGA53HFRN1")


def test_size_for_resolves_by_map():
    set_power_map({"breeze": {"20": 7, "07": 7}})
    assert size_for("breeze", "RAC-SN20HP.D07 (комплект)", btu=13, category_id=2) == 7


def test_size_for_zero_means_ignore_code():
    # btu_true=0 — владелец пометил «код не мощность» (ревизия .D07 и т.п.)
    set_power_map({"breeze": {"20": 7, "07": 0}})
    assert size_for("breeze", "RAC-SN20HP.D07", btu=13, category_id=2) == 7


def test_size_for_conflict_falls_back_to_btu():
    set_power_map({"breeze": {"07": 7, "12": 12}})
    # текст содержит оба кода с разными мощностями → не гадаем, фолбэк на btu_calc
    assert size_for("breeze", "AS-07UW плюс AS-12HR", btu=25, category_id=2) == 7  # 25 м² → 7


def test_size_for_unknown_source_falls_back():
    set_power_map({"daichi": {"25": 9}})
    assert size_for("breeze", "RAC-KD25HP.D03", btu=35, category_id=2) == 12      # 35 м² → 12


def test_size_for_no_map_behaves_like_size_from_btu():
    assert size_for("breeze", "что угодно", btu=9000, category_id=2) == 9


def test_load_power_map_yaml(tmp_path):
    p = tmp_path / "power_map.yaml"
    p.write_text(
        "breeze:\n  '25': {btu_now: 7, n: 9, btu_true: 7}\n"
        "  '35': {btu_now: 12, n: 19, btu_true: 12}\n"
        "  '17': {btu_now: 0, n: 1, btu_true: }\n"      # не заполнено → пропуск
        "daichi:\n  '53': 18\n",                          # короткая форма тоже ок
        encoding="utf-8")
    pm = load_power_map(p)
    assert pm == {"breeze": {"25": 7, "35": 12}, "daichi": {"53": 18}}
