"""Exercise main's real command/callback wiring, with all IO isolated."""
import importlib.util
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import openpyxl
from content_factory.config import load_config
from content_factory.bot.wizard import WizardStore
from content_factory.bot.wizard_flow import _category_key
from content_factory.bot.avito_categories import category_key
from content_factory.orchestrator.excel_pipeline import ExcelStore
from content_factory.orchestrator.generation import generation_enabled
from content_factory.ready_price import batch_status, batch_keys, sync_catalog


def test_real_main_controls_and_wizard(tmp_path, monkeypatch):
    from content_factory.bot import worker_control
    monkeypatch.setattr(worker_control, 'request_run', lambda: True)
    monkeypatch.setattr(worker_control, 'status_lines', lambda: ['Тестовый обработчик'])
    entry = os.getenv('BOT_ENTRYPOINT_UNDER_TEST')
    if entry:
        spec = importlib.util.spec_from_file_location('control_loop_candidate', entry)
        bot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bot)
    else:
        from content_factory.bot import run as bot
    config_file = tmp_path / 'config.yaml'
    config_file.write_text('{}', encoding='utf-8')
    cfg = load_config(config_file)
    cfg.state.db = str(tmp_path / 'state.db')
    cfg.state.card_jobs_db = str(tmp_path / 'cards.db')
    cfg.auto_tasks = []
    cfg.cards.dir = str(tmp_path / 'cards')
    cfg.telegram.review_channel_id = '123'
    values = {'TELEGRAM_BOT_TOKEN': 'TEST', 'TELEGRAM_OWNER_CHAT_ID': '123',
              'FOTOGEN_CHAT_ID': '123', 'FOTOGEN_API_TOKEN': 'TEST',
              'FOTOGEN_OUTPUT_DIR': str(tmp_path / 'output'),
              'FOTOGEN_QUEUE_DB': str(tmp_path / 'queue.db'),
              'READY_PRICE_CATALOG_DB': str(tmp_path / 'catalog.db')}
    monkeypatch.setattr(bot, 'config', lambda key, default=None: values.get(key, default))
    monkeypatch.setattr(bot, 'load_config', lambda _: cfg)
    monkeypatch.setenv('READY_PRICE_PUBLICATION_CONTROL', str(tmp_path / 'publication.json'))
    monkeypatch.setenv('VK_PLAN_STATE_DB', str(tmp_path / 'vk.db'))
    prices = tmp_path / 'prices'
    prices.mkdir()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(['№', 'Артикул', 'Бренд', 'Наименование', 'Цена (руб.)', 'Заказ (шт.)'])
    ws.append(['Стиральные машины', '', '', '', '', ''])
    ws.append(['1', '10', 'Beko', 'Стиральная машина Beko WSRE6512', '21990', ''])
    ws.append(['2', '11', 'Candy', 'Стиральная машина Candy CS4', '19990', ''])
    ws.append(['Телевизоры', '', '', '', '', ''])
    ws.append(['3', '20', 'MIU', 'Телевизор MIU H32 Smart', '8650', ''])
    wb.save(prices / 'manual.xlsx')
    messages = []
    def transport(req):
        assert req.url.host == 'api.telegram.org', 'No paid generation or external IO allowed'
        body = {k: v[0] for k, v in parse_qs(req.read().decode()).items()}
        if req.url.path.endswith('/sendMessage'):
            messages.append(body)
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': len(messages)+1}})
    client = httpx.Client(transport=httpx.MockTransport(transport))
    if hasattr(bot, 'telegram_client'):
        monkeypatch.setattr(bot, 'telegram_client', lambda *_: client)
    else:
        monkeypatch.setattr(bot.httpx, 'Client', lambda *args, **kw: client)
    class Finished(BaseException):
        pass
    def message(text, actor=123):
        return {'message': {'chat': {'id': actor}, 'from': {'id': actor}, 'text': text}}
    def callback(data, actor=123):
        return {'callback_query': {'id': 'test', 'from': {'id': actor}, 'data': data,
                                  'message': {'chat': {'id': actor}, 'message_id': 1}}}
    def disabled():
        assert not generation_enabled(cfg.state.db)
        assert 'недоступен' not in messages[-1]['text']
        assert 'generation:on' in messages[-1]['reply_markup']
    def enabled():
        assert generation_enabled(cfg.state.db)
        assert 'generation:off' in messages[-1]['reply_markup']
    def suppliers():
        assert WizardStore(cfg.state.db).snapshot('123').step == 'awaiting_source'
        assert 'wizard:source:' in messages[-1]['reply_markup']
        assert 'wizard:cat:' not in messages[-1]['reply_markup']
    def draft():
        assert WizardStore(cfg.state.db).snapshot('123').step == 'awaiting_category'
        assert 'wizard:cat:' in messages[-1]['reply_markup']
    def picks():
        assert WizardStore(cfg.state.db).snapshot('123').step == 'awaiting_pick'
        assert 'wizard:pick_first:1' in messages[-1]['reply_markup']
    def found():
        assert 'Candy' in messages[-1]['text']
        assert WizardStore(cfg.state.db).snapshot('123').step == 'awaiting_pick'
    def avito_categories():
        assert 'avito:cat:' in messages[-1]['reply_markup']
    def avito_choices():
        assert 'avito:go:' in messages[-1]['reply_markup']
    def gated():
        assert '/generation on' in messages[-1]['text']
        assert ExcelStore(cfg.state.db).by_status('new') == []
    def submitted():
        assert len(ExcelStore(cfg.state.db).by_status('new')) == 1
    def queue_is_current():
        assert 'Последняя выбранная партия' in messages[-1]['text']
        assert '1 товаров' in messages[-1]['text']
        assert 'excancel:latest' in messages[-1]['reply_markup']
    def only_latest_cancelled():
        assert 'отменено 1' in messages[-1]['text']
        assert ExcelStore(cfg.state.db).get('excel|beko|wsre6512').status == 'cancelled'
    def finite_batch():
        assert len(batch_keys(cfg.state.db)) == 1, messages[-1]['text']
        assert json.loads((tmp_path / 'publication.json').read_text())['enabled'] is True
    def paused():
        assert not batch_status(cfg.state.db)['enabled']
        assert json.loads((tmp_path / 'publication.json').read_text())['enabled'] is False
    # A fresh supplier fixture, never the live catalog.
    with sqlite3.connect(values['READY_PRICE_CATALOG_DB']) as con:
        stamp = datetime.now(timezone.utc).isoformat()
        con.execute('CREATE TABLE releases(source TEXT,status TEXT,generated_at TEXT,manifest TEXT)')
        con.execute('INSERT INTO releases VALUES(?,?,?,?)', ('telegram-nikita', 'accepted', stamp,
                    json.dumps({'schema_version': 2, 'snapshot_kind': 'full', 'provenance': {
                        'supplier_fallback': False, 'supplier': {'modified_at': stamp}}})))
        con.execute('CREATE TABLE catalog(source TEXT,article TEXT,sha256 TEXT,data TEXT,present INTEGER,missing_since TEXT)')
        con.execute('CREATE TABLE source_bindings(source TEXT,article TEXT,ad_id TEXT)')
        for i in range(3):
            data = {'article': f'A{i}', 'brand': 'BQ', 'name': f'Телевизор BQ M{i}',
                    'group': 'Телевизоры', 'bucket': 'small', 'avito_price': 1050,
                    'source_price': '1000', 'availability': 'supplier_price_present'}
            con.execute('INSERT INTO catalog VALUES(?,?,?,?,1,NULL)',
                        ('telegram-nikita', f'A{i}', 'test', json.dumps(data)))
    sync_catalog(values['READY_PRICE_CATALOG_DB'], cfg.state.db)
    sequence = [
        (message('/generation'), disabled),
        (callback('generation:on', actor=999), lambda: assert_disabled()),
        (callback('generation:on'), enabled),
        (message('/generation off'), disabled),
        (message('🎬 Контент-завод'), None),
        (message('🎛 Генерация'), disabled),
        (message('/task'), suppliers),
        (callback('wizard:source:' + _category_key('manual')), draft),
        (callback('wizard:cat:' + _category_key('manual') + ':0'), draft),
        (callback('wizard:cat:' + _category_key('manual') + ':' + _category_key('Стиральные машины')), picks),
        (callback('wizard:catpage:' + _category_key('manual') + ':0'), draft),
        (callback('wizard:cat:' + _category_key('manual') + ':' + _category_key('Стиральные машины')), picks),
        (message('/find'), None),
        (message('Candy'), found),
        (callback('wizard:pick_first:1'), None),
        (callback('wizard:time_now'), None),
        (callback('wizard:skip_photo'), None),
        (callback('wizard:skip_utp'), None),
        (callback('wizard:confirm'), gated),
        (message('/generation on'), enabled),
        (callback('wizard:confirm'), submitted),
        (message('📋 Очередь'), queue_is_current),
        (callback('excancel:latest'), only_latest_cancelled),
        (message('/task'), suppliers),
        (callback('wizard:source:' + _category_key('manual')), draft),
        (message('Beko'), picks),
        (callback('wizard:pick_first:1'), None),
        (callback('wizard:time_now'), None),
        (callback('wizard:skip_photo'), None),
        (callback('wizard:skip_utp'), None),
        (callback('wizard:confirm'), submitted),
        # Actual category buttons select one out of three eligible rows.
        (message('/avito categories'), avito_categories),
        (callback('avito:cat:' + category_key('Телевизоры')), avito_choices),
        (callback('avito:go:' + category_key('Телевизоры') + ':1'), finite_batch),
        (callback('avito:pause'), paused),
        (callback('avito:cancel'), paused),
        (callback('avito:restart'), finite_batch),
        (message('/avito pause'), paused),
        (message('/generation off'), disabled),
    ]
    def assert_disabled():
        assert not generation_enabled(cfg.state.db)
    position = 0
    previous_check = None
    def updates(*args, **kwargs):
        nonlocal position, previous_check
        if previous_check:
            previous_check()
        if position == len(sequence):
            raise Finished()
        update, previous_check = sequence[position]
        position += 1
        return [dict(update, update_id=position)]
    monkeypatch.setattr(bot, 'get_updates', updates)
    try:
        bot.main()
    except Finished:
        pass
    assert position == len(sequence)
    assert not generation_enabled(cfg.state.db)
    assert not batch_status(cfg.state.db)['enabled']
    assert not any('недоступен' in msg['text'] or 'внутренняя ошибка' in msg['text'] for msg in messages)
