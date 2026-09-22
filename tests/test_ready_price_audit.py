from content_factory.ready_price_audit import (
    OcrWord, find_repeated_feature_terms, find_unverified_terms,
)


def _ocr(*words):
    return [OcrWord(word, 96.0) for word in words]


def test_rejects_generated_function_absent_from_verified_facts():
    unknown = find_unverified_terms(
        _ocr("ЭЛЕКТРИЧЕСКАЯ", "МЯСОРУБКА", "ПРИГОТОВЛЕНИЕ", "КОЛБАСОК"),
        _ocr("Oursson"), brand="Oursson", model="MG5530/RD",
        name="Мясорубка Oursson MG5530/RD",
        features=["Номинальная мощность 500 Вт", "Система реверса"],
    )
    assert unknown == ["колбасок", "приготовление"]


def test_accepts_verified_words_source_labels_and_small_ocr_typo():
    unknown = find_unverified_terms(
        _ocr("ВАРОЧНАЯ", "ПОВЕРХНОСТЬ", "Артикул", "Газ-контроль", "онфоро", "Cotton"),
        _ocr("Cotton"), brand="il Monte", model="BH-678G",
        name="Газовая варочная поверхность il Monte BH-678G",
        features=["Газ-контроль конфорок"],
    )
    assert unknown == []


def test_rejects_a_second_copy_of_multiple_feature_labels():
    features = ["Диагональ экрана 43", "Разрешение 1920x1080",
                "Контрастность 3600:1", "Номинальная мощность 16 Вт"]
    repeated = find_repeated_feature_terms(
        _ocr("Диагональ", "Разрешение", "Контрастность", "Номинальная", "мощность",
             "Диагональ", "Разрешение", "Контрастность", "Номинальная", "мощность"),
        features,
    )
    assert repeated == ["диагональ", "контрастность", "мощность", "номинальная", "разрешение"]


def test_allows_one_repeated_label_as_a_heading():
    assert find_repeated_feature_terms(
        _ocr("Мощность", "Мощность", "Разрешение", "Контрастность"),
        ["Мощность 500 Вт", "Разрешение 1920x1080", "Контрастность 3000:1"],
    ) == []
