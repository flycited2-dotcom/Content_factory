from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

import pytest
from content_factory.ingest.excel_price import parse_price_xlsx
from content_factory.ingest.price_sync import sync_prices, SLOT


def catalog(path, *, fallback=False, days=0):
    stamp = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE releases(sha256 TEXT,source TEXT,status TEXT,generated_at TEXT,manifest TEXT)')
        db.execute('CREATE TABLE catalog(source TEXT,article TEXT,sha256 TEXT,data TEXT,present INTEGER)')
        manifest = {'schema_version': 2, 'snapshot_kind': 'full', 'original_filename': 'БытТехОпт_20261001.xlsx',
                    'issue_date': '2026-10-01', 'provenance': {'supplier_fallback': fallback,
                    'supplier': {'modified_at': stamp}}}
        db.execute('INSERT INTO releases VALUES(?,?,?,?,?)', ('release', 'telegram-nikita', 'accepted', stamp, json.dumps(manifest)))
        for article, availability, price, present, digest in [
            ('A', 'supplier_price_present', '1000', 1, 'release'),
            ('B', 'unverified_origin', '700', 1, 'release'),
            ('C', 'supplier_price_present', '300', 0, 'release'),
            ('D', 'supplier_price_present', '500', 1, 'previous')]:
            data = {'article': article, 'availability': availability, 'name': 'Телевизор BQ ' + article,
                    'brand': 'BQ', 'group': 'Телевизоры', 'source_price': price, 'avito_price': 1050}
            db.execute('INSERT INTO catalog VALUES(?,?,?,?,?)', ('telegram-nikita', article, digest, json.dumps(data), present))
    return path


def test_mirror_only_current_verified_rows_without_extra_markup(tmp_path):
    db = catalog(tmp_path / 'catalog.sqlite')
    prices = tmp_path / 'prices'
    result = sync_prices(db, prices)
    assert result['items'] == 1 and result['excluded'] == 1
    rows = parse_price_xlsx(prices / f'{SLOT}.xlsx')
    assert [(x.article, x.price) for x in rows] == [('A', 1000)]
    first = (prices / f'{SLOT}.xlsx').stat().st_mtime_ns
    assert sync_prices(db, prices)['status'] == 'unchanged'
    assert (prices / f'{SLOT}.xlsx').stat().st_mtime_ns == first


@pytest.mark.parametrize('fallback,days', [(True, 0), (False, 5)])
def test_untrusted_snapshot_cannot_replace_saved_price(tmp_path, fallback, days):
    prices = tmp_path / 'prices'
    prices.mkdir()
    target = prices / f'{SLOT}.xlsx'
    target.write_bytes(b'LAST GOOD')
    with pytest.raises(ValueError):
        sync_prices(catalog(tmp_path / 'catalog.sqlite', fallback=fallback, days=days), prices)
    assert target.read_bytes() == b'LAST GOOD'


def test_keep_preferences_when_new_supplier_slot_is_adopted(tmp_path):
    prices = tmp_path / 'prices'
    prices.mkdir()
    old = 'manual__быттехопт_20260704'
    (prices / 'markups.json').write_text(json.dumps({old: 7}), encoding='utf-8')
    (prices / 'telegram_sources.json').write_text(json.dumps({'disabled': [old, 'other']}), encoding='utf-8')
    sync_prices(catalog(tmp_path / 'catalog.sqlite'), prices)
    assert json.loads((prices / 'markups.json').read_text(encoding='utf-8'))[SLOT] == 7
    assert json.loads((prices / 'telegram_sources.json').read_text(encoding='utf-8'))['disabled'] == [old, 'other', SLOT]
    # Repeated refresh must not reverse an owner's later toggle.
    (prices / 'telegram_sources.json').write_text(json.dumps({'disabled': ['other']}), encoding='utf-8')
    sync_prices(tmp_path / 'catalog.sqlite', prices)
    assert json.loads((prices / 'telegram_sources.json').read_text(encoding='utf-8'))['disabled'] == ['other']


def test_empty_verified_release_preserves_last_good(tmp_path):
    db = catalog(tmp_path / 'catalog.sqlite')
    with sqlite3.connect(db) as con:
        con.execute("UPDATE catalog SET present=0 WHERE article='A'")
    prices = tmp_path / 'prices'
    prices.mkdir()
    target = prices / f'{SLOT}.xlsx'
    target.write_bytes(b'LAST GOOD')
    with pytest.raises(ValueError, match='empty_verified'):
        sync_prices(db, prices)
    assert target.read_bytes() == b'LAST GOOD'
