from decimal import Decimal
from content_factory.ingest.oasis_db import row_to_raw, build_query_params, CRIMEA_QUERY, group_tech_rows


def test_group_tech_rows():
    rows = [
        {"nc_code": "N1", "title": "Холод, кВт", "value": "3.5"},
        {"nc_code": "N1", "title": "Уровень шума", "value": "24 дБ"},
        {"nc_code": "N1", "title": "Пусто", "value": ""},          # пустое значение — пропуск
        {"nc_code": "N1", "title": "Холод, кВт", "value": "9.9"},  # дубль title — не перезаписывает
        {"nc_code": "N2", "title": "Тип", "value": "инвертор"},
    ]
    out = group_tech_rows(rows)
    assert out["N1"] == {"Холод, кВт": "3.5", "Уровень шума": "24 дБ"}
    assert out["N2"] == {"Тип": "инвертор"}


def test_row_to_raw_maps_columns():
    row = {"source": "rusklimat", "nc_code": "NC7", "brand": "Ballu",
           "title": "Ballu Olympio 07", "series": None, "category_id": 2,
           "btu_calc": 7, "price_wholesale": Decimal("30000"),
           "price_base": Decimal("21000"), "crimea_qty": 4, "image_url": "https://i/1.jpg"}
    r = row_to_raw(row)
    assert r.source == "rusklimat" and r.nc_code == "NC7"
    assert r.price_base == Decimal("21000") and r.stock_qty == 4
    assert r.image_urls == ["https://i/1.jpg"]


def test_row_to_raw_handles_null_image():
    r = row_to_raw({"source": "daichi", "nc_code": "N", "brand": "B", "title": "T",
                    "series": "S", "category_id": 6, "btu_calc": 60,
                    "price_wholesale": Decimal("1"), "price_base": None,
                    "crimea_qty": 0, "image_url": None})
    assert r.image_urls == []


def test_query_params_use_config():
    p = build_query_params(crimea="Симферополь", cats=[2, 6, 7], deny=["%мульти%"])
    assert p["crimea"] == "Симферополь" and p["cats"] == [2, 6, 7]


def test_query_text_targets_crimea_warehouse():
    assert "warehouse = %(crimea)s" in CRIMEA_QUERY
    assert "btu_calc > 0" in CRIMEA_QUERY
