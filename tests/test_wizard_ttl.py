"""Протухание брошенного визарда (жалоба 2026-07-10): владелец не завершил
«Свой товар» (бот ждал цену), диалог висел в wizard_state вечно — при каждом
заходе в чат бот трактовал любой текст как ответ на древний вопрос
(«вечный черновик» с ForceReply). Диалог без активности WIZARD_TTL_SECONDS
считается брошенным: snapshot чистит его и возвращает None."""
import sqlite3
import time

from content_factory.bot.wizard import WizardStore, WIZARD_TTL_SECONDS


def test_stale_dialog_expires_and_row_deleted(tmp_path):
    store = WizardStore(tmp_path / "w.db")
    store.start("1")
    ts0 = time.time()
    assert store.snapshot("1", now=ts0 + WIZARD_TTL_SECONDS + 1) is None
    with sqlite3.connect(tmp_path / "w.db") as c:
        assert c.execute("SELECT COUNT(*) FROM wizard_state").fetchone()[0] == 0


def test_fresh_dialog_survives(tmp_path):
    store = WizardStore(tmp_path / "w.db")
    store.start("1")
    st = store.snapshot("1", now=time.time() + WIZARD_TTL_SECONDS - 60)
    assert st is not None and st.step == "awaiting_category"


def test_legacy_row_without_ts_is_stale(tmp_path):
    # прод-строки до миграции (ts IS NULL) — как раз застрявший диалог МВО:
    # считаем брошенными, а не вечными
    store = WizardStore(tmp_path / "w.db")
    with sqlite3.connect(tmp_path / "w.db") as c:
        c.execute("INSERT INTO wizard_state(chat_id, step) VALUES('1', 'awaiting_manual_price')")
    assert store.snapshot("1") is None


def test_any_write_refreshes_ttl(tmp_path):
    store = WizardStore(tmp_path / "w.db")
    store.start("1")
    with sqlite3.connect(tmp_path / "w.db") as c:   # состарить диалог вручную
        c.execute("UPDATE wizard_state SET ts=1.0")
    store.set_manual_name("1", "Товар X")           # активность обновляет ts
    st = store.snapshot("1")
    assert st is not None and st.step == "awaiting_manual_price"
