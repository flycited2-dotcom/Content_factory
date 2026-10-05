import json
import openpyxl
from content_factory.bot.source_menu import sources_markup, toggle_tg_source, make_sources_fn, source_key
from content_factory.ingest.excel_price import tg_disabled


def price(path):
    wb = openpyxl.Workbook()
    wb.active.append(['№', 'Артикул', 'Бренд', 'Наименование', 'Цена'])
    wb.active.append([1, 'A1', 'BQ', 'Телевизор BQ A1', 1000])
    wb.save(path)


def test_old_number_buttons_cannot_toggle_another_supplier(tmp_path):
    price(tmp_path / 'manual__b.xlsx')
    buttons = sources_markup(tmp_path)['inline_keyboard'][0]
    callback = buttons[0]['callback_data']
    price(tmp_path / 'manual__a.xlsx')
    assert 'изменился' in toggle_tg_source(tmp_path, 'srctg:1')
    assert tg_disabled(tmp_path) == set()
    toggle_tg_source(tmp_path, callback)
    assert tg_disabled(tmp_path) == {'manual__b'}


def test_status_distinguishes_auto_price_from_manual_copy(tmp_path):
    price(tmp_path / 'manual__bt.xlsx')
    price(tmp_path / 'manual__other.xlsx')
    (tmp_path / 'source_refresh.json').write_text(json.dumps({'manual__bt': {
        'display_name': 'БытТехОпт (авто)', 'issue_date': '2026-10-01'}}), encoding='utf-8')
    text = make_sources_fn(tmp_path)()
    assert 'БытТехОпт (авто)' in text and 'прайс 2026-10-01' in text
    assert 'автообновление не подключено' in text
    assert 'только Avito' not in text


def test_deferred_refresh_is_visible_without_hiding_saved_prices(tmp_path):
    price(tmp_path / 'manual__bt.xlsx')
    (tmp_path / 'price-sync-status.json').write_text(json.dumps({'status': 'deferred'}), encoding='utf-8')
    text = make_sources_fn(tmp_path)()
    assert 'отложено' in text and '1 поз.' in text
