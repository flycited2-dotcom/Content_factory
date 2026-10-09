from content_factory.ingest.aru_site import parse_catalog_page


def test_hidden_price_and_explicit_stock_are_preserved():
    page = '''<div class="v-products-list__item" data-product-id="42">
    <a class="v-products-list__name" href="/tools/drill/">Дрель DCK</a>
    <span class="v-products-list-card__sku">Артикул - KJZ13</span>
    <span class="v-products-list-card__stock"><span class="_in-stock">В наличии</span></span>
    <img class="v-products-list__img" data-src="/photo.jpg">
    <tr class="v-products-list__features-tr"><td><span class="v-products-list__features-name">Бренд</span></td>
    <td><span class="v-products-list__features-value">DCK</span></td></tr></div>
    <ul class="pagination"><li><a href="/vse-tovary/?page=2">2</a></li></ul>'''
    items, pages = parse_catalog_page(page)
    assert pages == 2
    assert items[0]['id'] == '42'
    assert items[0]['article'] == 'KJZ13'
    assert items[0]['brand'] == 'DCK'
    assert items[0]['price'] is None
    assert items[0]['available'] is True
    assert items[0]['image_urls'] == ['https://aru.ooo/photo.jpg']


def test_missing_stock_is_unknown_and_unavailable_is_false():
    page = '''<div class="v-products-list__item" data-product-id="1">
    <a class="v-products-list__name" href="/a/">A</a></div>
    <div class="v-products-list__item" data-product-id="2">
    <a class="v-products-list__name" href="/b/">B</a>
    <span class="v-products-list-card__stock">Нет в наличии</span>
    <span class="v-products-list__price">1 234,50 ₽</span></div>'''
    items, _ = parse_catalog_page(page)
    assert items[0]['available'] is None
    assert items[1]['available'] is False
    assert items[1]['price'] == '1234.50'


def test_no_products_is_an_error_instead_of_empty_success():
    import pytest
    with pytest.raises(ValueError, match='no product'):
        parse_catalog_page('<html>Login required</html>')


def test_source_menu_shows_unpriced_catalog_without_usable_price(tmp_path):
    from content_factory.ingest.aru_site import save_snapshot
    from content_factory.bot.source_menu import make_sources_fn
    from datetime import datetime, timezone
    save_snapshot(tmp_path / 'aru-catalog.json', {
        'source': 'aru', 'complete': True, 'items': [{'id': '1', 'price': None}],
        'generated_at': datetime.now(timezone.utc).isoformat()})
    assert 'АРУ (aru.ooo): каталог 1 поз.; с ценой 0' in make_sources_fn(tmp_path)()


def test_repeated_page_does_not_produce_complete_snapshot():
    import pytest
    from content_factory.ingest.aru_site import crawl_catalog
    class Response:
        text = '''<div><div class="v-products-list__item" data-product-id="42">
        <a class="v-products-list__name" href="/drill/">Дрель</a></div>
        <ul class="pagination"><a href="?page=2">2</a></ul></div>'''
        def raise_for_status(self):
            pass
    class Client:
        def get(self, *args, **kwargs):
            return Response()
    with pytest.raises(ValueError, match='repeated catalog page'):
        crawl_catalog(Client(), delay=0)


def test_source_menu_accepts_existing_api_catalog_without_xlsx(tmp_path, monkeypatch):
    from content_factory.bot import source_menu
    monkeypatch.setattr(source_menu, 'load_price_slots', lambda path: [('splithub', [])])
    text = source_menu.make_sources_fn(tmp_path)()
    assert 'splithub: 0 поз.' in text
    assert 'каталог API' in text
