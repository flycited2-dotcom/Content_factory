"""Мастер-рубильник генерации контента.

Флаг хранится в общей state-БД Контент-завода. По умолчанию генерация
выключена: после деплоя или восстановления БД завод не должен сам наполнять
общую очередь фотоагента, пока владелец явно не даст ``/generation on``.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


_SETTING = "generation_enabled"
_EXCEL_ACTIVE = ("new", "research", "card")


def _settings_c(db) -> sqlite3.Connection:
    path = Path(db)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    return con


def generation_enabled(db) -> bool:
    """Без сохранённого разрешения генерация безопасно выключена."""
    with _settings_c(db) as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (_SETTING,)).fetchone()
    return bool(row) and row[0] == "1"


def set_generation_enabled(db, enabled: bool) -> None:
    with _settings_c(db) as con:
        con.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
            (_SETTING, "1" if enabled else "0"),
        )


@dataclass(frozen=True)
class StopSummary:
    queue_jobs: int = 0
    catalog_items: int = 0
    excel_items: int = 0


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def stop_factory_tail(state_db, card_jobs_db, queue_db) -> StopSummary:
    """Снять ожидающий хвост только Контент-завода.

    Чужие задания общей очереди определяются отсутствием ссылки из card_jobs и
    excel_items и не затрагиваются. ``processing`` не прерываем: ChatGPT уже
    начал работу; соответствующий item выключается и готовый результат не
    продвигается заводом дальше.
    """
    card_path = Path(card_jobs_db)
    state_path = Path(state_db)
    queue_path = Path(queue_db)

    catalog: list[tuple[str, str]] = []
    if card_path.exists():
        with sqlite3.connect(card_path) as con:
            if _table_exists(con, "card_jobs"):
                catalog = list(con.execute(
                    "SELECT key,input_filename FROM card_jobs WHERE status='pending'"
                ))

    excel: list[tuple[str, int | None, int | None]] = []
    if state_path.exists():
        with sqlite3.connect(state_path) as con:
            if _table_exists(con, "excel_items"):
                marks = ",".join("?" for _ in _EXCEL_ACTIVE)
                excel = list(con.execute(
                    f"SELECT key,research_job,card_job FROM excel_items "
                    f"WHERE status IN ({marks})", _EXCEL_ACTIVE
                ))

    cancelled_jobs = 0
    cancelled_inputs: set[str] = set()
    if queue_path.exists():
        with sqlite3.connect(queue_path) as con:
            if _table_exists(con, "jobs"):
                for _, input_filename in catalog:
                    cur = con.execute(
                        "UPDATE jobs SET status='cancelled' "
                        "WHERE input_filename=? AND status='pending'",
                        (input_filename,),
                    )
                    if cur.rowcount:
                        cancelled_inputs.add(input_filename)
                        cancelled_jobs += cur.rowcount
                for _, research_job, card_job in excel:
                    for job_id in (research_job, card_job):
                        if job_id is not None:
                            cancelled_jobs += con.execute(
                                "UPDATE jobs SET status='cancelled' "
                                "WHERE id=? AND status='pending'", (job_id,)
                            ).rowcount

    if card_path.exists() and cancelled_inputs:
        with sqlite3.connect(card_path) as con:
            for input_filename in cancelled_inputs:
                con.execute(
                    "UPDATE card_jobs SET status='cancelled' WHERE input_filename=?",
                    (input_filename,),
                )

    cancelled_excel = 0
    if state_path.exists() and excel:
        with sqlite3.connect(state_path) as con:
            marks = ",".join("?" for _ in _EXCEL_ACTIVE)
            cancelled_excel = con.execute(
                f"UPDATE excel_items SET status='cancelled' WHERE status IN ({marks})",
                _EXCEL_ACTIVE,
            ).rowcount

    return StopSummary(
        queue_jobs=cancelled_jobs,
        catalog_items=len(cancelled_inputs),
        excel_items=cancelled_excel,
    )


def generation_command(arg: str | None, state_db, card_jobs_db, queue_db) -> str:
    """Логика Telegram-команды ``/generation [on|off]``."""
    command = (arg or "").strip().lower()
    if command == "on":
        set_generation_enabled(state_db, True)
        return ("▶️ Генерация контента включена. Новые задания Контент-завода "
                "снова могут поступать фотоагенту.\nВыключить: /generation off")
    if command == "off":
        set_generation_enabled(state_db, False)
        summary = stop_factory_tail(state_db, card_jobs_db, queue_db)
        return ("⏸ Генерация контента выключена. Новые задания не создаются.\n"
                f"Снято из очереди фотоагента: {summary.queue_jobs}; "
                f"остановлено позиций каталога: {summary.catalog_items}; "
                f"Excel: {summary.excel_items}.\n"
                "Ручные задания фото-бота и Avito не затронуты.\n"
                "Включить: /generation on")
    if command:
        return "❌ формат: /generation on или /generation off"
    if generation_enabled(state_db):
        return ("▶️ Мастер-генерация: ВКЛЮЧЕНА\n"
                "Контент-завод может ставить research и карточки.\n"
                "Выключить: /generation off")
    return ("⏸ Мастер-генерация: ВЫКЛЮЧЕНА\n"
            "Контент-завод не ставит новые задания фотоагенту.\n"
            "Включить: /generation on")
