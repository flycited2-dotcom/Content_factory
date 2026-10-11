"""Ошибки excel-конвейера в Telegram: понятная причина + кнопка «повторить».

Жалоба владельца 2026-10-11: в личку приходило «❌ Новые ошибки конвейера прайса»
без причины по-русски и без действия. При этом страница товара у владельца в браузере
открывалась, фото и характеристики там были. Теперь: одно сообщение на позицию,
причина и совет, кнопка повтора; ручной повтор сбрасывает «уже сообщали»."""
import json
import sqlite3
import time

from content_factory.bot import run as botrun
from content_factory.orchestrator import excel_run
from content_factory.orchestrator.excel_pipeline import ExcelStore
from content_factory.publish.orders import OrderLinks

URL_403 = ("https://www.vseinstrumenti.ru/product/"
           "kompressor-vozdushnyj-bezmaslennyj-gruwatt-kr-50-2bm-21032840/")
ERR_403 = f"research: Client error '403 Forbidden' for url '{URL_403}'"
ERR_NO_MODEL = "research: research: source page does not contain exact model"


def _sender(monkeypatch, ok=True):
    sent = []

    def send(token, chat, message, http=None, reply_markup=None):
        sent.append((message, reply_markup))
        return ok

    monkeypatch.setattr(excel_run, "send_message", send)
    return sent


def _failed(tmp_path, key="ready-price|00-1", error=ERR_403, **fields):
    store = ExcelStore(tmp_path / "state.db")
    store.add_items([(key, "Gruwatt", "KR-50-2BM", "Компрессор KP-50-2БМ(220В", 22990)])
    store.update(key, status="failed", error=error, tries=2, research_job=7,
                 research_request_key="old", **fields)
    return store


# ── причина и совет ──────────────────────────────────────────────────────────
def test_describe_403_names_site_block_and_keeps_url():
    cause, advice, url = excel_run.describe_failure(ERR_403)
    assert "403" in cause and "робот" in cause
    assert "интернет" in advice          # это НЕ обрыв связи — владельцу надо знать
    assert url == URL_403                # без хвостовой кавычки из текста httpx


def test_describe_missing_model_collapses_doubled_prefix():
    cause, advice, url = excel_run.describe_failure(ERR_NO_MODEL)
    assert "точной модели" in cause
    assert url is None
    assert "research: research:" not in cause + advice


def test_describe_timeout_is_marked_temporary():
    cause, advice, _ = excel_run.describe_failure(
        "research: Locator.wait_for: Timeout 30000ms exceeded.\nCall log:")
    assert "время ожидания" in cause
    assert "временн" in advice
    assert "Call log" not in cause + advice


def test_describe_unknown_error_falls_back_to_raw_text():
    cause, _, _ = excel_run.describe_failure("странный сбой X")
    assert "странный сбой X" in cause


# ── одно сообщение на позицию, с кнопкой ─────────────────────────────────────
def test_alert_is_per_item_with_retry_button(tmp_path, monkeypatch):
    sent = _sender(monkeypatch)
    store = _failed(tmp_path)
    events = [("ready-price|00-1", "Компрессор KP-50-2БМ(220В", ERR_403)]
    assert excel_run.notify_new_failures(store.path, events, "tok", "chat") == 1
    (text, markup), = sent
    assert "Компрессор KP-50-2БМ(220В" in text
    assert URL_403 in text and "403" in text
    button = json.loads(markup)["inline_keyboard"][0][0]
    assert button["text"].startswith("🔎") and "заново" in button["text"]
    action, code = button["callback_data"].split(":", 1)
    assert action == "exretry"
    assert OrderLinks(store.path).key_for(code) == "ready-price|00-1"


def test_card_stage_error_offers_card_retry(tmp_path, monkeypatch):
    sent = _sender(monkeypatch)
    store = _failed(tmp_path, error="card audit: unverified_card_text:качество")
    excel_run.notify_new_failures(
        store.path, [("ready-price|00-1", "Компрессор", "card audit: unverified_card_text:качество")],
        "tok", "chat")
    button = json.loads(sent[0][1])["inline_keyboard"][0][0]
    assert button["text"].startswith("🎨")


def test_long_name_is_not_cut_in_the_middle_of_a_word(tmp_path, monkeypatch):
    sent = _sender(monkeypatch)
    store = _failed(tmp_path)
    name = "Компрессор воздушный безмасляный " + "очень " * 30 + "длинный"
    excel_run.notify_new_failures(store.path, [("ready-price|00-1", name, ERR_403)], "tok", "chat")
    first_line = sent[0][0].splitlines()[0]
    assert first_line.endswith("…") and len(first_line) < 140
    assert not first_line[:-1].endswith("оч")


def test_same_failure_is_not_repeated_until_owner_retries(tmp_path, monkeypatch):
    sent = _sender(monkeypatch)
    store = _failed(tmp_path)
    events = [("ready-price|00-1", "Компрессор", ERR_403)]
    assert excel_run.notify_new_failures(store.path, events, "tok", "chat") == 1
    assert excel_run.notify_new_failures(store.path, events, "tok", "chat") == 0
    assert store.retry_item("ready-price|00-1") == "retried"
    # повтор снова упал той же ошибкой — владелец ДОЛЖЕН увидеть новое сообщение
    assert excel_run.notify_new_failures(store.path, events, "tok", "chat") == 1
    assert len(sent) == 2


def test_unsent_alert_is_not_marked_and_burst_is_throttled(tmp_path, monkeypatch):
    store = _failed(tmp_path)
    events = [(f"ready-price|{n}", f"Товар {n}", ERR_NO_MODEL) for n in range(10)]
    _sender(monkeypatch, ok=False)
    assert excel_run.notify_new_failures(store.path, events, "tok", "chat") == 0
    sent = _sender(monkeypatch, ok=True)
    assert excel_run.notify_new_failures(store.path, events, "tok", "chat", limit=4) == 4
    assert excel_run.notify_new_failures(store.path, events, "tok", "chat", limit=4) == 4
    assert excel_run.notify_new_failures(store.path, events, "tok", "chat", limit=4) == 2
    assert len(sent) == 10                  # ни один не потерян и не продублирован


# ── повтор одной позиции ─────────────────────────────────────────────────────
def test_retry_item_resets_failed_to_clean_start(tmp_path):
    store = _failed(tmp_path, due_at=time.time() + 6 * 3600)
    assert store.retry_item("ready-price|00-1") == "retried"
    item = store.get("ready-price|00-1")
    assert (item.status, item.tries, item.error, item.research_job) == ("new", 0, None, None)
    with sqlite3.connect(store.path) as db:
        row = db.execute("SELECT research_request_key,due_at FROM excel_items").fetchone()
    assert row == (None, None)             # ждать таймер не нужно: запуск на ближайшем тике


def test_retry_item_skips_auto_retry_timer_but_keeps_pending_request(tmp_path):
    store = _failed(tmp_path)
    # sync_catalog уже вернул позицию в new с отложенным автоповтором (6 ч)
    store.update("ready-price|00-1", status="new", due_at=time.time() + 6 * 3600,
                 research_job=None, research_request_key="lost-response")
    assert store.retry_item("ready-price|00-1") == "started_now"
    with sqlite3.connect(store.path) as db:
        row = db.execute("SELECT status,due_at,research_request_key FROM excel_items").fetchone()
    # ключ запроса не трогаем: возможно платное задание уже принято очередью
    assert row == ("new", None, "lost-response")


def test_retry_item_ignores_items_already_moving(tmp_path):
    store = _failed(tmp_path)
    for status in ("research", "card", "preview", "cancelled"):
        store.update("ready-price|00-1", status=status)
        assert store.retry_item("ready-price|00-1") is None
        assert store.get("ready-price|00-1").status == status
    assert store.retry_item("нет-такого") is None


def test_retry_failed_command_also_forgets_old_alerts(tmp_path, monkeypatch):
    _sender(monkeypatch)
    store = _failed(tmp_path)
    events = [("ready-price|00-1", "Компрессор", ERR_403)]
    assert excel_run.notify_new_failures(store.path, events, "tok", "chat") == 1
    assert store.retry_failed() == 1
    assert excel_run.notify_new_failures(store.path, events, "tok", "chat") == 1


# ── бот: обработчик кнопки ───────────────────────────────────────────────────
def test_bot_retry_fn_reports_each_outcome(tmp_path):
    from content_factory.orchestrator.generation import set_generation_enabled
    store = _failed(tmp_path)
    set_generation_enabled(store.path, True)
    woken = []
    retry = botrun.make_retry_excel_fn(store.path, wake_fn=lambda: woken.append(1) or True)
    reply = retry("ready-price|00-1")
    assert reply.startswith("🔁") and "Компрессор" in reply
    assert store.get("ready-price|00-1").status == "new"
    assert woken == [1]                      # конвейер разбужен, ждать 10 минут не надо
    # повторное нажатие на ту же кнопку: позиция уже ждёт запуска
    assert retry("ready-price|00-1").startswith("⏳")
    store.update("ready-price|00-1", status="preview")
    assert retry("ready-price|00-1").startswith("ℹ️")
    assert retry("нет-такого").startswith("❌")


def test_bot_retry_fn_warns_when_generation_is_off(tmp_path):
    store = _failed(tmp_path)                # общая генерация по умолчанию выключена
    woken = []
    retry = botrun.make_retry_excel_fn(store.path, wake_fn=lambda: woken.append(1) or True)
    reply = retry("ready-price|00-1")
    assert reply.startswith("🔁") and "/generation on" in reply
    assert store.get("ready-price|00-1").status == "new" and woken == []
