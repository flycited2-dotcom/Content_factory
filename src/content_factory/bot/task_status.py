"""Show the selected cohort separately from the whole content queue."""
from collections import Counter
from datetime import datetime
from pathlib import Path
from contextlib import closing
import sqlite3


def cancel_known_job(queue_db, job_id, request_key):
    """Honor an owner stop even when the worker accepts the request afterwards."""
    if not queue_db or not Path(queue_db).is_file():
        return 0
    try:
        with closing(sqlite3.connect(Path(queue_db).resolve().as_uri() + '?mode=rw', uri=True)) as db, db:
            return db.execute("UPDATE jobs SET status='cancelled' WHERE id=? "
                              "AND request_key=? AND status='pending'", (job_id, request_key)).rowcount
    except sqlite3.Error:
        return 0


def recover_stopped_requests(state_db, queue_db):
    """An off timer may reconcile stopped requests, without starting a pipeline."""
    if not Path(state_db).is_file() or not queue_db or not Path(queue_db).is_file():
        return 0
    try:
        with closing(sqlite3.connect(Path(state_db).resolve().as_uri() + '?mode=ro', uri=True)) as db:
            found = db.execute("SELECT 1 FROM excel_items WHERE status='cancelled' AND "
                               "(COALESCE(research_request_key,'')<>'' OR COALESCE(card_request_key,'')<>'') "
                               "LIMIT 1").fetchone()
        if not found:
            return 0
    except sqlite3.Error:
        return 0
    from content_factory.orchestrator.excel_pipeline import ExcelStore
    return recover_submissions(ExcelStore(state_db), queue_db)


def recover_submissions(store, queue_db) -> int:
    """Attach a known request after a timeout; never send a generation request.

    Only the saved request identity is proof. Unknown requests remain stopped,
    and a concurrent owner cancellation wins over recovery.
    """
    with store._c() as db:
        rows = db.execute(
            "SELECT key,status,research_job,card_job,research_request_key,card_request_key "
            "FROM excel_items WHERE status IN ('submission','submission_failed','cancelled','new','research') "
            "AND (COALESCE(card_request_key,'')<>'' OR COALESCE(research_request_key,'')<>'')"
        ).fetchall()
    if not rows or not queue_db or not Path(queue_db).is_file():
        return 0
    recovered = 0
    try:
        with closing(sqlite3.connect(Path(queue_db).resolve().as_uri() + '?mode=rw', uri=True)) as queue, queue:
            columns = {row[1] for row in queue.execute('PRAGMA table_info(jobs)')}
            for key, old_status, research_job, card_job, research_request, card_request in rows:
                for stage, job_id, request_key in [('research', research_job, research_request),
                                                  ('card', card_job, card_request)]:
                    if not request_key:
                        continue
                    job = queue.execute(
                        "SELECT id,status,output_filename FROM jobs WHERE request_key=?",
                        (request_key,)).fetchone()
                    if not job or job[1] not in ('pending', 'processing', 'done', 'failed', 'cancelled'):
                        continue
                    if job_id is not None and job_id != job[0]:
                        continue
                    if 'result_sent' in columns:
                        queue.execute('UPDATE jobs SET result_sent=1 WHERE id=? AND request_key=?',
                                      (job[0], request_key))
                    terminal_error = job[1] in ('failed', 'cancelled') or (job[1] == 'done' and not job[2])
                    expected = ('new', 'research', 'submission', 'submission_failed') if stage == 'card' else ('new',)
                    with store._c() as db:
                        marks = ','.join('?' for _ in expected)
                        recovered += db.execute(
                            f"UPDATE excel_items SET status=CASE WHEN status='cancelled' "
                            f"THEN 'cancelled' ELSE ? END,{stage}_job=?,error=? "
                            f"WHERE key=? AND status IN ({marks},'cancelled') "
                            f"AND {stage}_job IS NULL AND {stage}_request_key=?",
                            ('submission_failed' if stage == 'card' and terminal_error
                             and old_status in ('submission', 'submission_failed') else stage,
                             job[0], f'manual_{stage}_job:{job[1]}' if terminal_error else None,
                             key, *expected, request_key)).rowcount
                    if store.get(key).status == 'cancelled':
                        queue.execute("UPDATE jobs SET status='cancelled' WHERE id=? "
                                      "AND request_key=? AND status='pending'", (job[0], request_key))
    except (sqlite3.Error, OSError):
        pass
    return recovered


def cancellable_items(store):
    # Include future scheduled work and unconfirmed photo submissions.
    with store._c() as db:
        keys = [row[0] for row in db.execute(
            "SELECT key FROM excel_items WHERE status IN "
            "('new','research','card','submission','submission_failed') ORDER BY ts,key")]
    return [item for key in keys if (item := store.get(key)) is not None]


def audit_task_event(state_db, action, keys=(), detail=None):
    """Persist owner control outcomes without tokens, messages or HTTP payloads."""
    import json
    import time
    with sqlite3.connect(state_db, timeout=30) as db:
        db.execute("CREATE TABLE IF NOT EXISTS bot_task_events "
                   "(id INTEGER PRIMARY KEY AUTOINCREMENT,ts REAL,action TEXT,"
                   "keys_json TEXT,detail_json TEXT)")
        db.execute("INSERT INTO bot_task_events(ts,action,keys_json,detail_json) "
                   "VALUES(?,?,?,?)", (time.time(), action, json.dumps(list(keys)),
                                       json.dumps(detail or {}, ensure_ascii=False)))


def selection_status_lines(store, queue_db=None) -> list[str]:
    receipt = store.latest_selection()
    if not receipt:
        return []
    items = [store.get(key) for key in receipt['keys']]
    jobs = {}
    job_ids = {item.card_job if item.status == 'card' else item.research_job
               for item in items if item and item.status in ('research', 'card')}
    job_ids.discard(None)
    if job_ids and queue_db and Path(queue_db).is_file():
        try:
            with closing(sqlite3.connect(Path(queue_db).resolve().as_uri() + '?mode=ro',
                                        uri=True, timeout=5)) as db:
                columns = {row[1] for row in db.execute('PRAGMA table_info(jobs)')}
                lane = 'assigned_account' if 'assigned_account' in columns else "''"
                marks = ','.join('?' for _ in job_ids)
                jobs = {row[0]: (row[1], row[2]) for row in db.execute(
                    f'SELECT id,status,{lane} FROM jobs WHERE id IN ({marks})', list(job_ids))}
        except (OSError, sqlite3.Error):
            pass
    counts = Counter(item.status if item else 'missing' for item in items)
    created = datetime.fromtimestamp(receipt['ts']).strftime('%d.%m %H:%M')
    lines = [f"Последняя выбранная партия №{receipt['id']} · {created}: {len(items)} товаров",
             f"🆕 ждут {counts['new']} · 🔎 поиск {counts['research']} · "
             f"🎨 карточки {counts['card']} · ✅ превью {counts['preview']} · "
             f"🛑 отменены {counts['cancelled']} · ❌ ошибки {counts['failed']}"]
    uncertain = counts['submission'] + counts['submission_failed']
    if uncertain:
        lines.append(f"⚠️ Отправка фотоагенту не подтверждена: {uncertain}. "
                     "Повторные задания не отправляются; проверяем очередь по номеру запроса.")
    if jobs:
        jobs_count = Counter(job[0] for job in jobs.values())
        lines.append(f"Фотоагенты: выполняются {jobs_count['processing']} · "
                     f"ждут дорожку {jobs_count['pending']} · "
                     f"результаты получены {jobs_count['done']}.")
    for item in items[:10]:
        if item:
            label = {'new': 'ожидает', 'research': 'поиск', 'card': 'карточка',
                     'preview': 'готово', 'cancelled': 'отменено', 'failed': 'ошибка',
                     'submission': 'проверка отправки',
                     'submission_failed': 'проверка отправки'}.get(item.status, item.status)
            job = jobs.get(item.card_job if item.status == 'card' else item.research_job)
            if item.status in ('research', 'card') and job:
                lane = {'acc1': 'A1', 'acc2': 'A2'}.get(job[1], job[1])
                detail = {'pending': 'ждёт дорожку', 'processing': 'в работе',
                          'done': 'результат получен', 'failed': 'ошибка фотоагента',
                          'cancelled': 'фотоагент остановлен'}.get(job[0], 'проверяю фотоагента')
                label += f" · {detail}" + (f" ({lane})" if lane else "")
            lines.append(f"• {item.name[:90]} — {label}")
            if item.error and item.status in ('new', 'research', 'card'):
                lines.append(f"  ⚠️ {item.error.splitlines()[0][:90]}; проверка будет повторена.")
    if len(items) > 10:
        lines.append(f"Ещё {len(items) - 10} в этой партии.")
    selected = set(receipt['keys'])
    with store._c() as db:
        others = [row[0] for row in db.execute(
            "SELECT key FROM excel_items WHERE key NOT LIKE 'ready-price|%' "
            "AND status IN ('new','research','card','submission','submission_failed')")
                  if row[0] not in selected]
    lines.append(f"Другие задания Контент-завода в работе: {len(others)}.")
    return lines
