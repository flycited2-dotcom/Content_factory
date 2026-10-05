"""Short database contention must not kill startup or repeat a paid stage."""
import sqlite3
import time
from threading import Event, Thread

import pytest

from content_factory import sqlite_state
from content_factory.bot.wizard import WizardStore
from content_factory.orchestrator.confirm_store import ConfirmStore
from content_factory.orchestrator.excel_pipeline import ExcelStore, tick
from content_factory.orchestrator.generation import (
    _settings_c, generation_enabled, set_generation_enabled)
from content_factory.publish.orders import OrderLinks
from content_factory.publish.telegram import PublishState


def test_confirm_store_startup_waits_for_six_second_writer_without_duplicate_submission(tmp_path):
    db = tmp_path / "state.db"
    ConfirmStore(db)
    locked = Event()
    released = Event()
    errors = []

    def hold_writer():
        try:
            with sqlite_state.state_connection(db) as connection:
                connection.execute("BEGIN EXCLUSIVE")
                locked.set()
                # The old default five-second SQLite timeout killed cf-excel here.
                time.sleep(6)
                connection.rollback()
        except BaseException as exc:
            errors.append(exc)
            locked.set()
        finally:
            released.set()

    writer = Thread(target=hold_writer)
    writer.start()
    try:
        assert locked.wait(5)
        assert not errors
        started = time.monotonic()
        ConfirmStore(db)
        assert time.monotonic() - started >= 5.5
        assert released.wait(5)
    finally:
        writer.join(timeout=10)
    assert not writer.is_alive() and not errors

    store = ExcelStore(db)
    store.select_items([("excel|beko|wm1", "Beko", "WM1", "Стиральная машина Beko WM1", 25000)])
    calls = []

    def submit_research(*args, **kwargs):
        calls.append(kwargs["request_key"])
        return 101

    def read_job(job_id):
        assert job_id == 101
        return "pending", None, None, None

    def forbidden(*args, **kwargs):
        raise AssertionError("No card or publication stage is expected.")

    tick(store, submit_research, read_job, forbidden, forbidden)
    tick(store, submit_research, read_job, forbidden, forbidden)
    assert len(calls) == 1
    assert store.get("excel|beko|wm1").research_job == 101


def test_state_connection_commits_rolls_back_and_closes_each_scope(tmp_path):
    db = tmp_path / "state.db"
    with sqlite_state.state_connection(db) as committed:
        committed.execute("CREATE TABLE sample(value TEXT)")
        committed.execute("INSERT INTO sample VALUES('committed')")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        committed.execute("SELECT 1")

    with pytest.raises(RuntimeError, match="cancel operation"):
        with sqlite_state.state_connection(db) as rolled_back:
            rolled_back.execute("INSERT INTO sample VALUES('must roll back')")
            raise RuntimeError("cancel operation")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        rolled_back.execute("SELECT 1")
    with sqlite_state.state_connection(db) as check:
        assert check.execute("SELECT value FROM sample").fetchall() == [("committed",)]


@pytest.mark.parametrize("store_type", [ConfirmStore, WizardStore, PublishState, OrderLinks])
def test_state_store_connections_close_and_use_thirty_second_busy_timeout(tmp_path, store_type):
    store = store_type(tmp_path / "state.db")
    with store._c() as connection:
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_generation_settings_context_commits_and_closes_without_changing_default(tmp_path):
    db = tmp_path / "state.db"
    assert generation_enabled(db) is False
    set_generation_enabled(db, True)
    assert generation_enabled(db) is True
    with _settings_c(db) as connection:
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_failed_settings_initialization_closes_its_connection(tmp_path, monkeypatch):
    opened = []
    real_connect = sqlite3.connect

    class BrokenSchemaConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql.startswith("CREATE TABLE"):
                raise sqlite3.OperationalError("schema initialization failed")
            return super().execute(sql, *args, **kwargs)

    def tracking_connect(*args, **kwargs):
        connection = real_connect(*args, factory=BrokenSchemaConnection, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite_state.sqlite3, "connect", tracking_connect)
    with pytest.raises(sqlite3.OperationalError, match="schema initialization failed"):
        generation_enabled(tmp_path / "state.db")
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
