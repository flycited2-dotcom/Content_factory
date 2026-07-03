"""Excel-прайс (БытТехОпт): парсер, извлечение модели, выбор позиций под /make."""
import openpyxl
from content_factory.ingest.excel_price import (
    parse_price_xlsx, extract_model, item_key, select_from_price)


def _xlsx(tmp_path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([""] * 6)                                    # пустая строка сверху (как в реале)
    ws.append(["№", "Артикул", "Бренд", "Наименование", "Цена (руб.)", "Заказ (шт.)"])
    for r in rows:
        ws.append(r)
    p = tmp_path / "price.xlsx"
    wb.save(p)
    return p


ROWS = [
    ["Холодильники с нижней морозильной камерой", "", "", "", "", ""],
    ["1", "1210", "Gorenje", "Холодильник Gorenje NRK6201ES4", "40451", ""],
    ["2", "1211", "Beko", "Холодильник Beko B1RCSK362S", "25990", ""],
    ["Стиральные машины с фронтальной загрузкой", "", "", "", "", ""],
    ["3", "1300", "Beko", "Стиральная машина Beko WSRE6512", "21990", ""],
    ["4", "1301", "Candy", "Стиральная машина Candy CS4", "19990", ""],
    ["Stinol", "", "", "", "", ""],
    ["5", "1400", "Stinol", "Холодильник Stinol STS 167 (167*60*62)", "20254", ""],
]


def test_parse_price_sections_and_items(tmp_path):
    items = parse_price_xlsx(_xlsx(tmp_path, ROWS))
    assert len(items) == 5
    assert items[0].section == "Холодильники с нижней морозильной камерой"
    assert items[0].brand == "Gorenje" and items[0].price == 40451
    assert items[2].section == "Стиральные машины с фронтальной загрузкой"
    assert items[4].brand == "Stinol" and items[4].section == "Stinol"


def test_parse_skips_rows_without_price(tmp_path):
    rows = ROWS + [["6", "1500", "X", "Товар без цены", None, ""]]
    items = parse_price_xlsx(_xlsx(tmp_path, rows))
    assert all(i.price for i in items)


def test_extract_model_real_names():
    assert extract_model("Холодильник Stinol STS 167 (167*60*62)", "Stinol") == "STS 167"
    assert extract_model("Встраиваемый холодильник Beko BCNA306E2S ( А+ )", "Beko") == "BCNA306E2S"
    assert extract_model("Холодильник Gorenje NRK6201ES4", "Gorenje") == "NRK6201ES4"
    assert extract_model("Просто товар без бренда", "Nope") == "Просто товар без бренда"


def test_item_key_matches_manual_pilot_keys(tmp_path):
    items = parse_price_xlsx(_xlsx(tmp_path, ROWS))
    stinol = [i for i in items if i.brand == "Stinol"][0]
    assert item_key(stinol) == "excel|stinol|sts 167"      # совпадает с ключами пилота


def test_select_quotas_and_rest(tmp_path):
    items = parse_price_xlsx(_xlsx(tmp_path, ROWS))
    got = select_from_price(items, "холодильник", {"beko": 1, "*": None}, 3, taken=set())
    brands = [i.brand.lower() for i in got]
    assert len(got) == 3 and brands.count("beko") == 1     # квота Beko + добор остальными
    assert not any("Стиральная" in i.name for i in got)    # категория держится (не стиралки)


def test_select_respects_taken_keys(tmp_path):
    items = parse_price_xlsx(_xlsx(tmp_path, ROWS))
    taken = {"excel|stinol|sts 167"}
    got = select_from_price(items, "холодильник", {}, 10, taken=taken)
    assert all(item_key(i) != "excel|stinol|sts 167" for i in got)


def test_select_category_filters_sections(tmp_path):
    items = parse_price_xlsx(_xlsx(tmp_path, ROWS))
    got = select_from_price(items, "стиральные", {}, 10, taken=set())
    assert {i.brand for i in got} == {"Beko", "Candy"}
