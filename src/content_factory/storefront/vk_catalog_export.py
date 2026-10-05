"""Public catalogue snapshot -> additive VK YML files. Never calls VK writes.

Image audit reads file headers (not the whole image) to check format/dimensions.
Only public retail prices are accepted. Source supplier keys stay stable.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html import unescape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import time
import unicodedata
from urllib.parse import quote, urlsplit
import urllib.request
import xml.etree.ElementTree as ET

from PIL import ImageFile

MAX_BYTES = 7_800_000  # below both decimal 8 MB and 8 MiB
GROUPS = {
    '01-classic-ac': 'Бытовые классические кондиционеры',
    '02-inverter-ac': 'Бытовые инверторные кондиционеры',
    '03-household-ac': 'Прочие бытовые сплит-системы',
    '04-semi-industrial': 'Полупромышленные кондиционеры',
    '05-mobile-window': 'Мобильные и оконные кондиционеры',
    '06-ventilation': 'Бризеры, вентиляция и рекуператоры',
    '07-air-care': 'Очистители, увлажнители, осушители и вентиляторы',
    '08-heaters': 'Обогреватели, конвекторы, пушки и завесы',
    '09-heating': 'Отопительные котлы и радиаторы',
    '10-water-heaters': 'Водонагреватели',
    '11-floor-heating': 'Тёплый пол',
    '12-other': 'Другие товары каталога',
}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'): self.hidden += 1
        if tag in ('p', 'br', 'li', 'div'): self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'): self.hidden = max(0, self.hidden - 1)
        if tag in ('p', 'li', 'div'): self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden: self.parts.append(data)


def clean_text(value):
    text = str(value or '')
    for _ in range(4):
        decoded = unescape(text)
        if decoded == text: break
        text = decoded
    parser = _Text()
    parser.feed(text)
    text = unescape(''.join(parser.parts))
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]', '', text)
    text = '\n'.join(re.sub(r'[ \t\xa0]+', ' ', line).strip() for line in text.splitlines())
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def model_identity(row):
    # Keep punctuation and numbers: similar series/capacities are NOT duplicates.
    brand = unicodedata.normalize('NFKC', row.get('brand', '')).strip().casefold()
    model = unicodedata.normalize('NFKC', row.get('articul', '')).strip().casefold()
    if brand and model and re.search(r'\d', model):
        return ('model', brand, re.sub(r'\s+', ' ', model))
    return ('title', brand, clean_text(row['title']).casefold())


def retail_price(row):
    try:
        value = Decimal(str(row.get('price')))
        if not value.is_finite() or value <= 0: return None
        return value.quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError):
        return None


def select_products(rows, excluded_ids):
    excluded_models = {model_identity(r) for r in rows if r['offer_id'] in excluded_ids}
    rejected, candidates = [], []
    for row in rows:
        reason = None
        if row['offer_id'] in excluded_ids or model_identity(row) in excluded_models:
            reason = 'previous_import_or_manual'
        elif retail_price(row) is None: reason = 'no_public_retail_price'
        elif row.get('currency', 'RUB') not in ('RUB', 'RUR', ''): reason = 'non_ruble_price'
        elif not row.get('images'): reason = 'no_image'
        elif not row.get('slug') or not clean_text(row.get('title')): reason = 'missing_public_fields'
        # Товар без остатка в витрину не идёт. Пометки available="false" мало:
        # VK такие позиции не скрывает, а показывает с плашкой «Недоступно», и
        # при полной выгрузке каталога 80% витрины оказывалось пустыми карточками.
        elif Decimal(row.get('quantity') or '0') <= 0: reason = 'not_in_stock'
        if reason:
            rejected.append({'id': row['offer_id'], 'title': row['title'], 'reason': reason})
        else: candidates.append(row)
    def priority(r):
        qty = Decimal(r.get('quantity') or '0')
        return (not (qty > 0 and r.get('warehouse') == 'Симферополь'), not qty > 0,
                retail_price(r), r['offer_id'])
    selected, models = [], {}
    for row in sorted(candidates, key=priority):
        identity = model_identity(row)
        if identity in models:
            rejected.append({'id': row['offer_id'], 'title': row['title'],
                             'reason': 'duplicate_model', 'kept_id': models[identity]})
        else:
            models[identity] = row['offer_id']
            selected.append(row)
    return selected, rejected


def group_key(row):
    cat = row['category_id']
    if cat == 2:
        title = row['title'].casefold()
        # Explicit evidence only: absence of 'inverter' does not mean classic.
        if re.search(r'инвертор|inverter', title) and not re.search(r'неинвертор|не инвертор', title):
            return '02-inverter-ac'
        if re.search(r'классическ|on[ /-]?off|неинвертор', title): return '01-classic-ac'
        for s in row.get('specs', []):
            if 'инвертор' in s['name'].lower():
                v = s['value'].strip().lower()
                if v in ('да', 'есть', 'true'): return '02-inverter-ac'
                if v in ('нет', 'false'): return '01-classic-ac'
        return '03-household-ac'
    if cat == 6: return '04-semi-industrial'
    if cat in (7, 107): return '05-mobile-window'
    if cat in (37, 40, 46): return '06-ventilation'
    if cat in (13, 14, 15, 120): return '07-air-care'
    if cat in (19, 21, 22, 24, 26, 27): return '08-heaters'
    if cat in (117, 118): return '09-heating'
    if cat in (30, 31): return '10-water-heaters'
    if cat == 119: return '11-floor-heating'
    return '12-other'


def export_category(row):
    """Avoid old feed ID collisions; repair only explicit source misclassifications."""
    title = row['title'].casefold()
    if re.match(r'терморегулятор\b', title):
        return 4, 'Терморегуляторы', '11-floor-heating'
    if row['category_id'] == 2:
        if re.match(r'радиатор\b', title):
            return 1118, 'Радиаторы отопления', '09-heating'
        if re.match(r'завеса тепловая|тепловая завеса', title):
            return 1027, 'Тепловые завесы', '08-heaters'
        if re.match(r'водонагреватель\b', title):
            return 2, 'Водонагреватели и бойлеры', '10-water-heaters'
        if re.match(r'блок ротации|подставка|кольцо соедин|плата |экран |средство моющ|'
                    r'рама монтаж|набор присоедин|панель для|полотенцедержатель|пульт |модуль |'
                    r'кронштейн|насос дренаж|фильтр |сифон|заглушка|клапан', title):
            return 1901, 'Комплектующие и расходные материалы', '12-other'
        if re.match(r'кондиционер оконный|оконный кондиционер', title):
            return 1107, 'Оконные кондиционеры', '05-mobile-window'
        if re.match(r'приточно-|вентиляционная установка|установка приточно-', title):
            return 1037, 'Бытовые вентиляционные установки', '06-ventilation'
        if re.search(r'^система кондиционирования воздуха \((внутренний|внешний) блок\)', title):
            return 1902, 'Отдельные блоки систем кондиционирования', '12-other'
    if row['category_id'] == 22: return 1, 'Масляные радиаторы', '08-heaters'
    if row['category_id'] in (30, 31): return 2, 'Водонагреватели и бойлеры', '10-water-heaters'
    if row['category_id'] == 119: return 3, 'Тёплые полы', '11-floor-heating'
    return 1000 + row['category_id'], row['category'], group_key(row)


def description(row):
    title = clean_text(row['title'])
    original = clean_text(row.get('description'))
    # No private supplier references or invented promises. Specs come from site.
    pieces = [title]
    if original:
        if len(original) > 1800:
            original = original[:1800].rsplit(' ', 1)[0] + '…'
        pieces.append(original)
    specs = []
    seen = set()
    for spec in row.get('specs', []):
        name, value = clean_text(spec['name']), clean_text(spec['value'])
        if not name or not value or name.casefold() in seen: continue
        if any(x in name.casefold() for x in ('поставщик', 'оптов', 'закупоч', 'штрихкод', 'ean')): continue
        if len(name) > 100 or len(value) > 160: continue
        seen.add(name.casefold())
        unit = clean_text(spec.get('unit', ''))
        suffix = f' {unit}' if unit and unit.casefold() not in value.casefold() else ''
        specs.append(f'• {name}: {value}{suffix}')
        if len(specs) == 8: break
    if specs: pieces.append('Характеристики:\n' + '\n'.join(specs))
    qty = Decimal(row.get('quantity') or '0')
    if qty > 0 and row.get('warehouse') == 'Симферополь':
        pieces.append('В наличии в Симферополе на дату выгрузки.')
    else:
        pieces.append('Под заказ. Наличие и срок поставки уточняйте при обращении.')
    pieces.append('Подберём технику под вашу задачу. Подробнее и заявка — в карточке товара на Split Home.')
    return '\n\n'.join(pieces)[:3900]


_UPPER_TRANSLIT = {
    'А': 'A', 'Б': 'B', 'В': 'V', 'Г': 'G', 'Д': 'D', 'Е': 'E', 'Ё': 'E', 'Ж': 'Zh',
    'З': 'Z', 'И': 'I', 'Й': 'Y', 'К': 'K', 'Л': 'L', 'М': 'M', 'Н': 'N', 'О': 'O',
    'П': 'P', 'Р': 'R', 'С': 'S', 'Т': 'T', 'У': 'U', 'Ф': 'F', 'Х': 'H', 'Ц': 'C',
    'Ч': 'Ch', 'Ш': 'Sh', 'Щ': 'Sch', 'Ъ': '', 'Ы': 'Y', 'Ь': '', 'Э': 'E', 'Ю': 'Yu',
    'Я': 'Ya',
}
# Латиницу не трогаем: меняется только кириллица, регистр остальных символов
# сохраняется, иначе поменялись бы уже выданные идентификаторы вроде breeze:1.
_ID_TRANSLIT = str.maketrans({
    **_UPPER_TRANSLIT,
    **{cyr.lower(): lat.lower() for cyr, lat in _UPPER_TRANSLIT.items() if lat},
})


def ascii_offer_id(value: str) -> str:
    """Артикул только латиницей: VK показывает кириллицу в id как «РќРЎ».

    Идентификатор должен остаться стабильным между выгрузками — повторный
    импорт обновляет товар по нему, а не создаёт второй.
    """
    text = (value or '').translate(_ID_TRANSLIT)
    return ''.join(ch for ch in text if ch.isascii())


def render_offer(row, image_url):
    qty = Decimal(row.get('quantity') or '0')
    node = ET.Element('offer', id=ascii_offer_id(row['offer_id']),
                      available='true' if qty > 0 else 'false')
    title = clean_text(row['title'])
    brand = clean_text(row.get('brand'))
    if brand and brand.casefold() not in title.casefold(): title = f'{brand} {title}'
    if len(title) > 100:
        model = clean_text(row.get('articul'))
        title = (f'{brand} {model}' if model else title[:99] + '…')[:100]
    for name, value in [
        ('url', 'https://splithome.ru/product/' + quote(row['slug'], safe='') + '/'),
        ('price', str(retail_price(row))), ('currencyId', 'RUR'),
        ('categoryId', str(row['category_id'])), ('picture', image_url),
        ('name', title), ('description', description(row)),
    ]:
        ET.SubElement(node, name).text = value
    return node


def serialize_feed(nodes, categories=None):
    root = ET.Element('yml_catalog', date=datetime.now().strftime('%Y-%m-%d %H:%M'))
    shop = ET.SubElement(root, 'shop')
    ET.SubElement(shop, 'name').text = 'БытТехОпт | Split Home'
    ET.SubElement(shop, 'company').text = 'БытТехОпт'
    ET.SubElement(shop, 'url').text = 'https://splithome.ru/'
    ET.SubElement(ET.SubElement(shop, 'currencies'), 'currency', id='RUR', rate='1')
    cats = ET.SubElement(shop, 'categories')
    used = sorted({n.findtext('categoryId') for n in nodes}, key=int)
    for cat in used:
        ET.SubElement(cats, 'category', id=cat).text = (categories or {}).get(cat, f'Раздел {cat}')
    offers = ET.SubElement(shop, 'offers')
    offers.extend(nodes)
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def partition_feeds(nodes, categories=None, max_bytes=MAX_BYTES):
    # Reserve the actual XML/category wrapper. Element byte sizes are additive.
    parts, current, size = [], [], 0
    wrapper = len(serialize_feed([], categories)) + sum(
        len(ET.tostring(ET.Element('category', id=k), encoding='utf-8')) + len(v.encode('utf-8')) + 20
        for k, v in (categories or {}).items()) + 500
    for node in nodes:
        node_bytes = len(ET.tostring(node, encoding='utf-8'))
        if node_bytes + wrapper >= max_bytes: raise ValueError('Single offer exceeds byte limit')
        if current and size + node_bytes + wrapper >= max_bytes:
            parts.append(serialize_feed(current, categories))
            current, size = [], 0
        current.append(node)
        size += node_bytes
    if current: parts.append(serialize_feed(current, categories))
    if any(len(p) >= max_bytes for p in parts): raise ValueError('Partition exceeds byte limit')
    return parts


def probe_image(url):
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.hostname not in {
        'rkcdn.ru', 'images.breez.ru', 'daichi.business', 'mdv-aircond.ru', 'mhi-aircond.ru', 'splithome.ru'
    }:
        return {'ok': False, 'reason': 'unapproved_image_url'}
    url = quote(url, safe=':/?=&%+@,;~-_.')
    parser = ImageFile.Parser()
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'SplitHome-Catalog-Validator/1.0',
                                                   'Range': 'bytes=0-65535'})
        with urllib.request.urlopen(req, timeout=12) as response:
            for _ in range(16):
                chunk = response.read(4096)
                if not chunk: break
                parser.feed(chunk)
                if parser.image:
                    width, height = parser.image.size
                    fmt = parser.image.format
                    ok = fmt in ('JPEG', 'PNG') and min(width, height) >= 400 and max(width, height) <= 20 * min(width, height)
                    return {'ok': ok, 'reason': 'ok' if ok else 'image_format_or_size',
                            'width': width, 'height': height, 'format': fmt, 'url': url,
                            'status': response.status}
        return {'ok': False, 'reason': 'image_header_unreadable'}
    except Exception as exc:
        return {'ok': False, 'reason': type(exc).__name__, 'detail': str(exc)[:180]}


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--snapshot', type=Path, required=True)
    cli.add_argument('--existing-feed', type=Path, required=True)
    cli.add_argument('--output', type=Path, required=True)
    cli.add_argument('--exclude-id', action='append', default=[])
    cli.add_argument('--workers', type=int, default=16)
    args = cli.parse_args()
    rows = json.loads(args.snapshot.read_bytes())
    excluded = {o.get('id') for o in ET.fromstring(args.existing_feed.read_bytes()).findall('./shop/offers/offer')}
    excluded.update(args.exclude_id)
    selected, rejected = select_products(rows, excluded)
    args.output.mkdir(parents=True, exist_ok=True)
    cache_path = args.output / 'image-audit.json'
    cache = json.loads(cache_path.read_text(encoding='utf-8')) if cache_path.exists() else {}
    print(json.dumps({'catalog': len(rows), 'candidates': len(selected),
                      'excluded': dict(Counter(r['reason'] for r in rejected))}), flush=True)
    def check(urls):
        todo = sorted(set(urls) - cache.keys())
        print(f'Checking {len(todo)} image URLs', flush=True)
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(probe_image, url): url for url in todo}
            done = 0
            last_report = time.monotonic()
            for future in as_completed(futures):
                cache[futures[future]] = future.result()
                done += 1
                if done % 250 == 0 or time.monotonic() - last_report > 30:
                    cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf-8')
                    print(f'Images {done}/{len(todo)}; valid {sum(r["ok"] for r in cache.values())}', flush=True)
                    last_report = time.monotonic()
        cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf-8')
    check(r['images'][0] for r in selected)
    failed_first = [r for r in selected if not cache[r['images'][0]]['ok']]
    check(url for r in failed_first for url in r['images'][1:4])
    transient = [url for url, result in cache.items()
                 if result['reason'] in ('URLError', 'TimeoutError', 'ConnectionResetError')
                 or (result['reason'] == 'HTTPError'
                     and re.search(r'HTTP Error 5\d\d', result.get('detail', '')))]
    for url in transient:
        del cache[url]
    check(transient)
    groups = {k: [] for k in GROUPS}
    accepted = []
    categories = {}
    for row in selected:
        valid = next((cache[u] for u in row['images'][:4] if u in cache and cache[u]['ok']), None)
        if valid is None:
            rejected.append({'id': row['offer_id'], 'title': row['title'], 'reason': 'no_valid_image',
                             'checks': [cache.get(u) for u in row['images'][:4]]})
            continue
        category_id, category_name, group = export_category(row)
        categories[str(category_id)] = category_name
        groups[group].append(render_offer({**row, 'category_id': category_id}, valid['url']))
        accepted.append(row['offer_id'])
    manifest = []
    for key, nodes in groups.items():
        for i, payload in enumerate(partition_feeds(nodes, categories), 1):
            name = f'{key}-{i:02}.yml'
            (args.output / name).write_bytes(payload)
            count = len(ET.fromstring(payload).findall('./shop/offers/offer'))
            manifest.append({'file': name, 'group': GROUPS[key], 'count': count, 'bytes': len(payload)})
    assert len(accepted) == len(set(accepted))
    assert len(accepted) + len(excluded) <= 15000, 'VK shop total capacity would be exceeded'
    report = {'catalog_count': len(rows), 'exported_count': len(accepted), 'files': manifest,
              'excluded_ids': sorted(excluded), 'reasons': dict(Counter(r['reason'] for r in rejected)),
              'rejected': rejected, 'image_validation': 'HTTP GET prefix; JPEG/PNG header dimensions; no full decode',
              'vk_import_executed': False}
    (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k not in ('rejected', 'excluded_ids')}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
