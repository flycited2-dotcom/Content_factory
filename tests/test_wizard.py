"""WizardStore — состояние пошагового диалога постановки задачи (/task) на chat_id.
Чистая логика без Telegram; переживает рестарт бота (SQLite, не память процесса).
Флоу v2 (2026-07-07): категория → выбор из автосписка (или свой список) → время
(«сейчас»/расписание) → (для «сейчас») фото → УТП → подтверждение."""
from content_factory.bot.wizard import WizardStore


def test_start_creates_awaiting_category(tmp_path):
    s = WizardStore(tmp_path / "w.db")
    s.start("123")
    st = s.snapshot("123")
    assert st.step == "awaiting_category"
    assert st.category is None and st.lines is None and st.candidates is None


def test_no_active_wizard_returns_none(tmp_path):
    s = WizardStore(tmp_path / "w.db")
    assert s.snapshot("999") is None


def test_autolist_path_transitions(tmp_path):
    s = WizardStore(tmp_path / "w.db")
    s.start("1")
    cands = [("excel|lg|x", "LG", "X", "Холодильник LG X", 30000),
             ("excel|beko|y", "Beko", "Y", "Холодильник Beko Y", 25000)]
    s.set_candidates("1", "холодильники", cands)
    st = s.snapshot("1")
    assert st.step == "awaiting_pick" and st.category == "холодильники"
    assert len(st.candidates) == 2

    s.set_pick("1", [list(cands[0])])
    st = s.snapshot("1")
    assert st.step == "awaiting_time"
    assert st.candidates == [list(cands[0])]        # остались только выбранные


def test_manual_list_path_goes_to_time(tmp_path):
    s = WizardStore(tmp_path / "w.db")
    s.start("1")
    s.set_category("1", "чайники")
    s.set_list("1", ["Чайник X"])
    assert s.snapshot("1").step == "awaiting_time"  # v2: после списка — время


def test_time_now_opens_photo_step(tmp_path):
    s = WizardStore(tmp_path / "w.db")
    s.start("1")
    s.set_category("1", "чайники")
    s.set_list("1", ["Чайник X"])
    s.set_time("1", None)                           # «🚀 сейчас»
    st = s.snapshot("1")
    assert st.step == "awaiting_photo" and st.due_at is None
    s.set_photo("1", None)
    s.set_utp("1", None)
    assert s.snapshot("1").step == "awaiting_confirm"


def test_scheduled_time_skips_photo_utp(tmp_path):
    s = WizardStore(tmp_path / "w.db")
    s.start("1")
    s.set_category("1", "чайники")
    s.set_list("1", ["Чайник X"])
    s.set_time("1", 1900000000.0)                   # будущее время
    st = s.snapshot("1")
    assert st.step == "awaiting_confirm"            # фото/УТП пропущены
    assert st.due_at == 1900000000.0


def test_cancel_resets_wizard(tmp_path):
    s = WizardStore(tmp_path / "w.db")
    s.start("1")
    s.set_category("1", "чайники")
    s.cancel("1")
    assert s.snapshot("1") is None


def test_survives_reopening_same_db(tmp_path):
    path = tmp_path / "w.db"
    s1 = WizardStore(path)
    s1.start("1")
    s1.set_candidates("1", "тв", [("k", "b", "m", "n", 1)])
    s2 = WizardStore(path)                          # имитация рестарта процесса
    st = s2.snapshot("1")
    assert st.step == "awaiting_pick" and st.candidates == [["k", "b", "m", "n", 1]]


def test_migrates_old_table_without_new_columns(tmp_path):
    # прод-таблица wizard_state существует без candidates_json/due_at/ts — ALTER
    # не падает; строка БЕЗ ts = брошенный до миграции диалог → протух (2026-07-10),
    # а новый диалог в мигрированной таблице работает штатно
    import sqlite3
    db = tmp_path / "w.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE wizard_state (chat_id TEXT PRIMARY KEY, step TEXT, "
                  "category TEXT, lines_json TEXT, photo_path TEXT, utp_text TEXT)")
        c.execute("INSERT INTO wizard_state(chat_id, step) VALUES('1', 'awaiting_category')")
    s = WizardStore(db)
    assert s.snapshot("1") is None                     # legacy-строка протухла
    s.start("1")
    st = s.snapshot("1")
    assert st.step == "awaiting_category" and st.due_at is None


def test_start_resets_previous_active_wizard(tmp_path):
    s = WizardStore(tmp_path / "w.db")
    s.start("1")
    s.set_candidates("1", "старая", [("k", "b", "m", "n", 1)])
    s.start("1")                                    # начали заново
    st = s.snapshot("1")
    assert st.step == "awaiting_category"
    assert st.category is None and st.candidates is None and st.due_at is None
