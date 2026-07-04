"""WizardStore — состояние пошагового диалога постановки задачи (/task) на chat_id.
Чистая логика без Telegram; переживает рестарт бота (SQLite, не память процесса)."""
from content_factory.bot.wizard import WizardStore


def test_start_creates_awaiting_category(tmp_path):
    s = WizardStore(tmp_path / "w.db")
    s.start("123")
    st = s.snapshot("123")
    assert st.step == "awaiting_category"
    assert st.category is None and st.lines is None


def test_no_active_wizard_returns_none(tmp_path):
    s = WizardStore(tmp_path / "w.db")
    assert s.snapshot("999") is None


def test_full_happy_path_transitions(tmp_path):
    s = WizardStore(tmp_path / "w.db")
    s.start("1")
    s.set_category("1", "стиральные машины")
    st = s.snapshot("1")
    assert st.step == "awaiting_list" and st.category == "стиральные машины"

    s.set_list("1", ["Модель A", "Модель B"])
    st = s.snapshot("1")
    assert st.step == "awaiting_photo" and st.lines == ["Модель A", "Модель B"]

    s.set_photo("1", "manual_1.png")
    st = s.snapshot("1")
    assert st.step == "awaiting_utp" and st.photo_path == "manual_1.png"

    s.set_utp("1", "УТП текст")
    st = s.snapshot("1")
    assert st.step == "awaiting_confirm" and st.utp_text == "УТП текст"


def test_skip_photo_and_utp_store_none(tmp_path):
    s = WizardStore(tmp_path / "w.db")
    s.start("1")
    s.set_category("1", "чайники")
    s.set_list("1", ["Чайник X"])
    s.set_photo("1", None)                 # пропуск
    st = s.snapshot("1")
    assert st.photo_path is None and st.step == "awaiting_utp"
    s.set_utp("1", None)                   # пропуск
    st = s.snapshot("1")
    assert st.utp_text is None and st.step == "awaiting_confirm"


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
    s1.set_category("1", "холодильники")
    s2 = WizardStore(path)                 # имитация рестарта процесса
    st = s2.snapshot("1")
    assert st.step == "awaiting_list" and st.category == "холодильники"


def test_separate_chats_are_independent(tmp_path):
    s = WizardStore(tmp_path / "w.db")
    s.start("1")
    s.set_category("1", "телевизоры")
    s.start("2")
    assert s.snapshot("2").step == "awaiting_category"
    assert s.snapshot("1").category == "телевизоры"     # чат 1 не задет


def test_start_resets_previous_active_wizard(tmp_path):
    s = WizardStore(tmp_path / "w.db")
    s.start("1")
    s.set_category("1", "старая категория")
    s.set_list("1", ["X"])
    s.start("1")                           # начали заново
    st = s.snapshot("1")
    assert st.step == "awaiting_category"
    assert st.category is None and st.lines is None
