from PIL import Image

from content_factory.storefront.product_posts import CatalogItem
from content_factory.storefront.tile_card import card_tiles, render_tile_card, repaint_price


def _item(attrs, name="Сплит-система Ballu BSO-12HN8"):
    return CatalogItem(id="rusklimat:NS-1", url="https://splithome.ru/x", price=39090, group="ac",
                       category="Сплит-системы", picture="", name=name, brand="Ballu",
                       prose="", attrs=attrs)


def test_tiles_come_only_from_characteristics():
    tiles = card_tiles(_item({"Номинальная производительность охлаждения": "2.2 кВт",
                              "Для помещения площадью, м2": "25",
                              "Инверторная технология": "Да",
                              "Тепловой насос": "нет"}))
    assert tiles == [("2.2 кВт", "охлаждение"), ("25 м²", "площадь помещения"),
                     ("Инвертор", "плавная работа")]


def test_card_needs_two_tiles_and_price_can_be_repainted(tmp_path):
    photo = tmp_path / "p.jpg"
    Image.new("RGB", (400, 400), "white").save(photo)
    assert render_tile_card(_item({}), 1000, photo, tmp_path / "none.jpg") is None
    out = render_tile_card(_item({"Номинальная производительность охлаждения": "2.2 кВт",
                                  "Срок эксплуатации": "7 лет"}), 39090, photo, tmp_path / "c.jpg")
    assert out is not None and Image.open(out).size == (1080, 1080)
    assert repaint_price(out, 41000)
