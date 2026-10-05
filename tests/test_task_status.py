import sqlite3

from content_factory.bot.task_status import recover_submissions, selection_status_lines
from content_factory.bot.run import make_cancel_excel_fn, excel_cancel_markup
from content_factory.orchestrator.excel_pipeline import ExcelStore
from content_factory.publish.orders import OrderLinks


def _queue(tmp_path):
    path = tmp_path / 'jobs.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY,status TEXT,request_key TEXT,output_filename TEXT)')
        db.executemany('INSERT INTO jobs VALUES(?,?,?,?)',
                       [(1, 'pending', 'confirmed', ''), (2, 'processing', 'processing', ''),
                        (3, 'done', 'done', 'card.png'), (4, 'done', 'empty', ''),
                        (5, 'pending', 'unrelated', '')])
    return path


def _reserved(store, key, request, status='submission_failed'):
    store.select_items([(key, 'Brand', key, key, 100)], reservation=True)
    store.update(key, status=status, card_request_key=request)


def test_recovery_attaches_only_known_requests_without_new_jobs(tmp_path):
    store = ExcelStore(tmp_path / 'state.db')
    queue = _queue(tmp_path)
    for key, request in [('excel|1', 'confirmed'), ('excel|2', 'processing'),
                         ('excel|3', 'done'), ('excel|4', 'empty'), ('excel|5', 'missing')]:
        _reserved(store, key, request)
    assert recover_submissions(store, queue) == 4
    assert [store.get(f'excel|{n}').card_job for n in range(1, 6)] == [1, 2, 3, 4, None]
    assert store.get('excel|4').status == 'submission_failed'
    with sqlite3.connect(queue) as db:
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 5
        assert db.execute('SELECT status FROM jobs WHERE id=5').fetchone()[0] == 'pending'
    assert recover_submissions(store, queue) == 0


def test_recovery_respects_cancel_and_cancels_late_pending_job(tmp_path):
    store = ExcelStore(tmp_path / 'state.db')
    queue = _queue(tmp_path)
    _reserved(store, 'excel|1', 'confirmed', 'cancelled')
    _reserved(store, 'excel|2', 'processing', 'cancelled')
    assert recover_submissions(store, queue) == 2
    assert store.get('excel|1').status == store.get('excel|2').status == 'cancelled'
    with sqlite3.connect(queue) as db:
        assert db.execute('SELECT status FROM jobs WHERE id=1').fetchone()[0] == 'cancelled'
        assert db.execute('SELECT status FROM jobs WHERE id=2').fetchone()[0] == 'processing'
    assert 'excel|1' not in store.selection_blocked_keys(queue)
    assert 'excel|2' in store.selection_blocked_keys(queue)


def test_missing_queue_never_creates_file_or_releases_uncertain_request(tmp_path):
    store = ExcelStore(tmp_path / 'state.db')
    _reserved(store, 'excel|1', 'unknown')
    missing = tmp_path / 'missing.db'
    assert recover_submissions(store, missing) == 0
    assert not missing.exists()
    assert store.get('excel|1').status == 'submission_failed'


def test_latest_selection_distinguishes_old_jobs_and_scoped_cancel(tmp_path):
    store = ExcelStore(tmp_path / 'state.db')
    store.add_items([('excel|old', 'Old', 'Old', 'Старая задача', 100)])
    store.select_items([(f'excel|{n}', 'LG', str(n), f'Стиральная машина LG {n}', 200)
                        for n in range(6)])
    text = '\n'.join(selection_status_lines(store))
    assert '6 товаров' in text and 'Другие задания Контент-завода в работе: 1' in text
    markup = excel_cancel_markup(store.path, OrderLinks(store.path))
    flat = [button for row in markup['inline_keyboard'] for button in row]
    assert any(b['text'] == '🛑 Отменить последнюю партию (6)' for b in flat)
    assert any(b['text'] == '🛑 Отменить всю очередь (7)' for b in flat)
    reply = make_cancel_excel_fn(store.path, '')('latest')
    assert 'отменено 6' in reply
    assert store.get('excel|old').status == 'new'
    assert 'отменены 6' in '\n'.join(selection_status_lines(store))
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT action FROM bot_task_events").fetchone()[0] == 'cancel'


def test_cancel_includes_future_schedule_and_unknown_submission(tmp_path):
    store = ExcelStore(tmp_path / 'state.db')
    store.select_items([('excel|future', '', '', 'Будущая задача', 100)], due_at=9999999999)
    _reserved(store, 'excel|uncertain', 'unknown')
    reply = make_cancel_excel_fn(store.path, tmp_path / 'missing.db')('*')
    assert 'отменено 2' in reply
    assert 'остановка не подтверждена' in reply
    assert not (tmp_path / 'missing.db').exists()
    assert store.get('excel|future').status == store.get('excel|uncertain').status == 'cancelled'


def test_lost_research_response_is_cancelled_by_identity_and_silenced(tmp_path):
    from content_factory.orchestrator.generation import generation_command
    store = ExcelStore(tmp_path / 'state.db')
    queue = _queue(tmp_path)
    with sqlite3.connect(queue) as db:
        db.execute('ALTER TABLE jobs ADD COLUMN result_sent INTEGER DEFAULT 0')
    store.select_items([('excel|research', 'B', 'M', 'Товар', 100)])
    store.update('excel|research', research_request_key='confirmed')
    generation_command('off', store.path, tmp_path / 'cards.db', queue)
    assert store.get('excel|research').status == 'cancelled'
    assert store.get('excel|research').research_job == 1
    with sqlite3.connect(queue) as db:
        assert db.execute('SELECT status,result_sent FROM jobs WHERE id=1').fetchone() == ('cancelled', 1)
        assert db.execute('SELECT status,result_sent FROM jobs WHERE id=5').fetchone() == ('pending', 0)
