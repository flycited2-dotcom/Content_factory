"""Подготовка YML только для отказавших товаров. Не отправляет данные в VK."""
from __future__ import annotations

import argparse
import csv
import io
from pathlib import Path
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET


def failed_offer_ids(error_csv: str) -> list[str]:
    reader = csv.DictReader(io.StringIO(error_csv.lstrip('\ufeff')))
    if not reader.fieldnames or 'Артикул' not in reader.fieldnames:
        raise ValueError('VK error log must contain Артикул column')
    # VK экранирует артикул начальным апострофом для просмотра в Excel.
    ids = list(dict.fromkeys(
        row['Артикул'].strip().removeprefix("'")
        for row in reader if (row.get('Артикул') or '').strip()
    ))
    if not ids:
        raise ValueError('No failed offer IDs; refusing to export entire catalog')
    return ids


def build_retry_feed(source: bytes, failed_ids: list[str],
                     picture_urls: dict[str, str]) -> bytes:
    selected = set(failed_ids)
    if not selected or set(picture_urls) - selected:
        raise ValueError('Picture overrides must belong to selected failed offers')
    if len(source) > 8 * 1024 * 1024:
        raise ValueError('Source exceeds 8 MiB')
    if b'<!DOCTYPE' in source.upper() or b'<!ENTITY' in source.upper():
        raise ValueError('XML declarations of entities are not supported')
    root = ET.fromstring(source)
    offers = root.find('./shop/offers')
    if root.tag != 'yml_catalog' or offers is None:
        raise ValueError('Expected yml_catalog/shop/offers')
    ids = [offer.get('id', '') for offer in offers.findall('offer')]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate source offer IDs')
    if selected - set(ids):
        raise ValueError('Failed offers not found in source: ' + ', '.join(sorted(selected - set(ids))))
    for offer in list(offers):
        key = offer.get('id', '')
        if key not in selected:
            offers.remove(offer)
            continue
        if key in picture_urls:
            url = picture_urls[key]
            parts = urlsplit(url)
            if (parts.scheme != 'https' or not parts.hostname
                    or parts.username or parts.password or parts.fragment):
                raise ValueError('Image override must be a public HTTPS URL without credentials')
            pictures = offer.findall('picture')
            if not pictures:
                raise ValueError(f'{key}: missing original picture element')
            pictures[0].text = url
            for extra in pictures[1:]:
                offer.remove(extra)
        for tag in ('name', 'description', 'picture', 'price'):
            if not (offer.findtext(tag) or '').strip():
                raise ValueError(f'{key}: missing required {tag}')
    ET.indent(root, space='  ')
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--errors', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--picture', action='append', default=[], metavar='OFFER_ID=HTTPS_URL')
    args = parser.parse_args()
    if args.output.resolve() in {args.source.resolve(), args.errors.resolve()}:
        parser.error('Output must not overwrite source feed or error log')
    overrides = {}
    for override in args.picture:
        key, separator, url = override.partition('=')
        if not separator or not key or key in overrides:
            parser.error('--picture requires a unique OFFER_ID=HTTPS_URL')
        overrides[key] = url
    ids = failed_offer_ids(args.errors.read_text(encoding='utf-8-sig'))
    payload = build_retry_feed(args.source.read_bytes(), ids, overrides)
    # Не перезаписываем предыдущую выгрузку, даже если путь введён ошибочно.
    with args.output.open('xb') as handle:
        handle.write(payload)
    print(f'Retry prepared: {len(ids)} offer(s), {args.output}. No VK import performed.')


if __name__ == '__main__':
    main()
