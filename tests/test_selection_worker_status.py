import sqlite3
from content_factory.orchestrator.excel_pipeline import ExcelStore
from content_factory.bot.task_status import recover_submissions, selection_status_lines


def test_status_distinguishes_pending_and_processing_lanes(tmp_path):
    store = ExcelStore(tmp_path / 'state.db')
    store.select_items([('excel|s', 'S', 'M', 'Samsung', 1), ('excel|l', 'L', 'N', 'LG', 2)])
    store.update('excel|s', status='card', card_job=10)
    store.update('excel|l', status='research', research_job=11)
    queue = tmp_path / 'queue.db'
    with sqlite3.connect(queue) as db:
        db.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY,status TEXT,assigned_account TEXT)')
        db.executemany('INSERT INTO jobs VALUES(?,?,?)', [(10, 'processing', 'acc1'), (11, 'pending', 'acc2')])
    status = '\n'.join(selection_status_lines(store, queue))
    assert 'выполняются 1' in status and 'ждут дорожку 1' in status
    assert 'Samsung — карточка · в работе (A1)' in status
    assert 'LG — поиск · ждёт дорожку (A2)' in status


def test_recover_ordinary_card_response_after_research(tmp_path):
    store = ExcelStore(tmp_path / 'state.db')
    store.select_items([('excel|s', 'S', 'M', 'Samsung', 1)])
    store.update('excel|s', status='research', research_job=1, card_request_key='same-request')
    queue = tmp_path / 'queue.db'
    with sqlite3.connect(queue) as db:
        db.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY,status TEXT,request_key TEXT,output_filename TEXT)')
        db.execute("INSERT INTO jobs VALUES(10,'pending','same-request',NULL)")
    assert recover_submissions(store, queue) == 1
    assert store.get('excel|s').status == 'card'
    assert store.get('excel|s').card_job == 10
    assert recover_submissions(store, queue) == 0


def test_recover_ordinary_terminal_card_uses_pipeline_failure_stage(tmp_path):
    store = ExcelStore(tmp_path / 'state.db')
    store.select_items([('excel|s', 'S', 'M', 'Samsung', 1)])
    store.update('excel|s', card_request_key='same-request')
    queue = tmp_path / 'queue.db'
    with sqlite3.connect(queue) as db:
        db.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY,status TEXT,request_key TEXT,output_filename TEXT)')
        db.execute("INSERT INTO jobs VALUES(10,'failed','same-request',NULL)")
    assert recover_submissions(store, queue) == 1
    assert store.get('excel|s').status == 'card'
    assert store.get('excel|s').card_job == 10
