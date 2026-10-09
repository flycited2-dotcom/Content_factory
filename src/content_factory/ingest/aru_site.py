"""АРУ: полный снимок каталога; скрытые цены и неизвестные остатки не угадываем."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import parse_qs, urljoin, urlparse

BASE_URL = 'https://aru.ooo/'
CATALOG_URL = BASE_URL + 'vse-tovary/'


def _nodes(node, cls):
    return node.xpath('.//*[contains(concat(" ",normalize-space(@class)," "), $cls)]',
                      cls=' ' + cls + ' ')


def _text(node, cls):
    found = _nodes(node, cls)
    return ' '.join(found[0].text_content().split()) if found else ''


def _price(text):
    raw = re.sub(r'[^0-9,.]', '', text).replace(',', '.')
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    return str(value) if value.is_finite() and value > 0 else None


def parse_catalog_page(page: str) -> tuple[list[dict], int]:
    from lxml import html
    doc = html.fromstring(page)
    items = []
    for card in _nodes(doc, 'v-products-list__item'):
        identity = card.get('data-product-id', '')
        names = _nodes(card, 'v-products-list__name')
        if not identity.isdigit() or not names:
            raise ValueError('aru_site: invalid product identity')
        name = ' '.join(names[0].text_content().split())
        url = urljoin(BASE_URL, names[0].get('href', ''))
        if not name or urlparse(url).netloc != 'aru.ooo':
            raise ValueError('aru_site: invalid product name or URL')
        specs = {}
        for row in _nodes(card, 'v-products-list__features-tr'):
            key = _text(row, 'v-products-list__features-name')
            value = _text(row, 'v-products-list__features-value')
            if key and value:
                specs[key] = value
        stock_text = _text(card, 'v-products-list-card__stock').casefold()
        available = False if 'нет в наличии' in stock_text else (
            True if 'в наличии' in stock_text else None)
        photos = []
        for img in _nodes(card, 'v-products-list__img'):
            url_img = urljoin(BASE_URL, img.get('data-src') or img.get('src') or '')
            if urlparse(url_img).scheme == 'https' and urlparse(url_img).netloc == 'aru.ooo':
                photos.append(url_img)
        items.append({
            'id': identity, 'article': re.sub(r'^Артикул\s*[-:]\s*', '',
                                            _text(card, 'v-products-list-card__sku')),
            'name': name, 'brand': specs.get('Бренд', ''), 'url': url,
            'category_path': urlparse(url).path.strip('/').split('/')[:-1],
            'available': available, 'price': _price(_text(card, 'v-products-list__price')),
            'price_basis': 'site_price' if _text(card, 'v-products-list__price') else 'hidden',
            'image_urls': list(dict.fromkeys(photos)), 'specifications': specs,
            'description': _text(card, 'v-products-list__text'),
        })
    if not items:
        raise ValueError('aru_site: no products found; authentication or layout changed')
    pages = [1]
    for group in _nodes(doc, 'pagination'):
        for link in group.xpath('.//a[@href]'):
            value = parse_qs(urlparse(link.get('href')).query).get('page', ['1'])[0]
            if value.isdigit():
                pages.append(int(value))
    return items, max(pages)


def crawl_catalog(client, *, delay: float = 1.0, max_pages: int = 1000) -> dict:
    items = {}
    expected_pages = 1
    page_number = 1
    while page_number <= expected_pages:
        response = client.get(CATALOG_URL, params={'page': page_number})
        response.raise_for_status()
        rows, pages = parse_catalog_page(response.text)
        if pages > max_pages:
            raise ValueError('aru_site: catalog exceeds page limit; snapshot not saved')
        if page_number == 1:
            expected_pages = pages
        elif pages != expected_pages:
            raise ValueError('aru_site: page count changed during crawl; retry required')
        fresh = [row for row in rows if row['id'] not in items]
        if not fresh:
            raise ValueError('aru_site: repeated catalog page; incomplete snapshot')
        for row in rows:
            items[row['id']] = row
        if page_number < expected_pages:
            time.sleep(delay)
        page_number += 1
    return {'schema_version': 1, 'source': 'aru', 'complete': True,
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'pages': expected_pages, 'items': list(items.values())}


def save_snapshot(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.aru-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def catalog_status(prices_dir: Path) -> str:
    """Показывать подключённый каталог даже до получения персонального прайса."""
    path = Path(prices_dir) / 'aru-catalog.json'
    if not path.is_file():
        return ''
    try:
        if path.stat().st_size > 50 * 1024 * 1024:
            raise ValueError('oversized')
        payload = json.loads(path.read_text(encoding='utf-8'))
        if payload.get('source') != 'aru' or payload.get('complete') is not True:
            raise ValueError('incomplete')
        stamp = datetime.fromisoformat(payload['generated_at'])
        if stamp.tzinfo is None:
            raise ValueError('timezone missing')
        rows = payload['items']
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError('invalid items')
        priced = sum(row.get('price') is not None for row in rows)
        overdue = (datetime.now(timezone.utc) - stamp).total_seconds() > 7 * 86400
        updated = stamp.astimezone(timezone(timedelta(hours=3))).strftime('%d.%m.%Y %H:%M МСК')
    except (OSError, ValueError, KeyError, TypeError):
        return '⚪ АРУ (aru.ooo): снимок каталога требует обновления.'
    if payload.get('authenticated') is True and payload.get('price_basis') == 'account_price':
        return (f'АРУ (aru.ooo): в наличии с персональной ценой {priced} поз.\n'
                f'   Цена контента: цена аккаунта +10%, целые рубли вверх. '
                f'\n   Прайс от {updated}; обновление по понедельникам. '
                f'{"Обновление задерживается; действует последний прайс." if overdue else "Выбор публикаций — отдельно."}')
    return (f'⚪ АРУ (aru.ooo): каталог {len(rows)} поз.; с ценой {priced}.\n'
            f'   ожидается прайс личного кабинета '
            'и выбор товаров. Поиск по прайсу и публикация пока не включены.')


def main():
    import httpx
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with httpx.Client(timeout=40, follow_redirects=True,
                      headers={'User-Agent': 'ContentFactory supplier catalog'}) as client:
        payload = crawl_catalog(client)
    save_snapshot(args.output, payload)
    print(json.dumps({'status': 'saved', 'items': len(payload['items']),
                      'pages': payload['pages'],
                      'priced': sum(row['price'] is not None for row in payload['items'])}))


if __name__ == '__main__':
    main()
