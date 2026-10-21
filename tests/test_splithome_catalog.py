"""Источник splithome.ru: весь ассортимент в наличии, цена — дилерский вход +10% до …90, без дублей splithub."""
from decimal import Decimal

from content_factory.ingest import splithome_catalog as home
from content_factory.ingest.excel_price import load_price_slots


def _row(source, nc, title, brand, category, wholesale, base=None):
    return {"source": source, "nc_code": nc, "title": title, "brand": brand, "category": category,
            "price_wholesale": Decimal(str(wholesale)), "price_base": Decimal(str(base or wholesale)), "quantity": 3}


ROWS = [
    _row("breeze", "НС-1", "Классическая сплит-система серии ROYAL FRESH RCI-RFS35HN", "ROYAL CLIMA",
         "Бытовые сплит-системы", 63890),                                          # розница в БД
    _row("breeze", "НС-2", "Водонагреватель накопительный ZANUSSI ZWH/S 50", "ZANUSSI",
         "Накопительные водонагреватели", 12990),
    _row("daichi", "D-1", "Midea MSAG2-09N8C2S-I/MSAG2-09N8C2S-O", "Midea", "Бытовые сплит-системы", 23395.42),
    _row("rusklimat", "R-1", "Радиатор панельный Royal Thermo VC22-500-600", "Royal Thermo",
         "Радиаторы отопления", 6000, base=5491.37),
    _row("breeze", "НС-3", "Конвектор без входа в API", "Ballu", "Конвекторы", 5990),
]


def test_round_up_to_90():
    assert home.round_up_90(52140) == 52190 and home.round_up_90(52190) == 52190 and home.round_up_90(52191) == 52290


def test_snapshot_prices_dealer_plus_10_and_skips():
    snap = home.build_snapshot(ROWS, {"НС-1": 47400, "НС-2": 8000}, skip_codes={"RCI-RFS35HN"})
    by_sku = {e["sku"]: e for e in snap}
    assert "НС-1" not in by_sku                               # уже приходит со splithub.ru
    assert "НС-3" not in by_sku                               # у Бриза нет входа — розницу не публикуем
    assert by_sku["НС-2"]["price"] == 8890                    # 8000 × 1,10 = 8800 → …90
    assert by_sku["D-1"]["price"] == 25790                    # 23 395,42 × 1,10 = 25 735 → 25 790
    assert by_sku["R-1"]["price"] == 6090                     # Русклимат: складская цена 5 491,37
    assert by_sku["НС-2"]["name"] == "Водонагреватель накопительный ZANUSSI ZWH/S 50"
    assert home.product_name({"title": "Сплит-система серии PANDORA", "brand": "ROYAL CLIMA"}) == \
        "ROYAL CLIMA Сплит-система серии PANDORA"


def test_slot_appears_in_price_slots_without_extra_markup(tmp_path):
    home.write_snapshot(tmp_path, home.build_snapshot(ROWS, {"НС-2": 8000}, set()))
    slots = dict(load_price_slots(tmp_path, for_telegram=True))
    items = {i.article: i for i in slots[home.SLOT]}
    assert items["НС-2"].price == 8890 and items["НС-2"].section == "Накопительные водонагреватели"
