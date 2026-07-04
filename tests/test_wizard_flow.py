"""Оркестрация визарда /task: категория → список моделей → (опц.) фото →
(опц.) УТП → подтверждение. Чистая логика (без Telegram) — download/send
инъецируются извне, тут — переходы и запись в ExcelStore/фотоагент."""
import openpyxl
from content_factory.bot.wizard import WizardStore
from content_factory.bot.wizard_flow import make_wizard_flow
from content_factory.orchestrator.excel_pipeline import ExcelStore


def _price(tmp_path, rows, fname="manual.xlsx"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["№", "Артикул", "Бренд", "Наименование", "Цена (руб.)", "Заказ (шт.)"])
    for r in rows:
        ws.append(r)
    prices = tmp_path / "prices"
    prices.mkdir(exist_ok=True)
    wb.save(prices / fname)
    return prices


ROWS = [
    ["Стиральные машины", "", "", "", "", ""],
    ["1", "10", "Beko", "Стиральная машина Beko WSRE6512", "21990", ""],
    ["2", "11", "Candy", "Стиральная машина Candy CS4", "19990", ""],
]


def _flow(tmp_path, submit_card=None, saved_photos=None, excel_fn=None):
    prices = _price(tmp_path, ROWS)
    store = WizardStore(tmp_path / "wizard.db")
    saved = saved_photos if saved_photos is not None else []

    def save_photo(chat_id, photo_bytes):
        p = tmp_path / f"photo_{chat_id}.jpg"
        p.write_bytes(photo_bytes)
        saved.append(str(p))
        return str(p)

    calls = []

    def _submit_card(brand, model, utp, photo_path):
        calls.append((brand, model, utp, photo_path))
        return submit_card(brand, model, utp, photo_path) if submit_card else 999

    start, handle_text, handle_photo, handle_callback = make_wizard_flow(
        tmp_path / "state.db", prices, store, _submit_card, save_photo,
        excel_fn=excel_fn or (lambda: "СТАТУС"))
    return start, handle_text, handle_photo, handle_callback, calls, store


def test_start_asks_category(tmp_path):
    start, *_ = _flow(tmp_path)
    r = start("1")
    assert "категория" in r.text.lower()


def test_start_includes_status_button(tmp_path):
    start, *_ = _flow(tmp_path)
    r = start("1")
    assert r.markup is not None
    flat = [b for row in r.markup["inline_keyboard"] for b in row]
    assert any(b["callback_data"] == "wizard:status" for b in flat)


def test_status_callback_matches_excel_fn_without_active_wizard(tmp_path):
    _, _, _, handle_callback, *_ = _flow(tmp_path, excel_fn=lambda: "research 2 | card 1")
    r = handle_callback("1", "wizard:status")
    assert r.text == "research 2 | card 1"


def test_status_callback_does_not_disrupt_active_wizard(tmp_path):
    start, handle_text, _, handle_callback, _, store = _flow(
        tmp_path, excel_fn=lambda: "research 2 | card 1")
    start("1")
    handle_text("1", "стиральные машины")
    r = handle_callback("1", "wizard:status")
    assert r.text == "research 2 | card 1"
    assert store.snapshot("1").step == "awaiting_list"     # диалог не сброшен


def test_handle_text_none_when_no_active_wizard(tmp_path):
    _, handle_text, *_ = _flow(tmp_path)
    assert handle_text("1", "что угодно") is None


def test_full_happy_path_no_overrides(tmp_path):
    start, handle_text, handle_photo, handle_callback, calls, store = _flow(tmp_path)
    start("1")
    handle_text("1", "стиральные машины")
    r = handle_text("1", "Стиральная машина Beko WSRE6512\nСтиральная машина Candy CS4")
    assert "найдено 2 из 2" in r.text
    r = handle_callback("1", "wizard:skip_photo")
    assert "УТП" in r.text
    r = handle_callback("1", "wizard:skip_utp")
    assert "Подтвердить" in r.text and r.markup is not None
    r = handle_callback("1", "wizard:confirm")
    assert "поставлено" in r.text and "2" in r.text

    es = ExcelStore(tmp_path / "state.db")
    items = es.by_status("new")
    assert {i.name for i in items} == {"Стиральная машина Beko WSRE6512",
                                       "Стиральная машина Candy CS4"}
    assert calls == []                          # без override submit_card не звался
    assert store.snapshot("1") is None          # диалог завершён и сброшен


def test_photo_override_skips_research_and_calls_submit_card(tmp_path):
    start, handle_text, handle_photo, handle_callback, calls, store = _flow(tmp_path)
    start("1")
    handle_text("1", "стиральные машины")
    handle_text("1", "Стиральная машина Beko WSRE6512")
    r = handle_photo("1", b"PHOTOBYTES")
    assert "УТП" in r.text
    handle_callback("1", "wizard:skip_utp")
    r = handle_callback("1", "wizard:confirm")
    assert "поставлено" in r.text

    es = ExcelStore(tmp_path / "state.db")
    items = es.by_status("card")
    assert len(items) == 1 and items[0].name == "Стиральная машина Beko WSRE6512"
    assert len(calls) == 1
    brand, model, utp, photo_path = calls[0]
    assert brand == "Beko" and utp == "" and photo_path.endswith("photo_1.jpg")


def test_photo_and_utp_override_both_used(tmp_path):
    start, handle_text, handle_photo, handle_callback, calls, store = _flow(tmp_path)
    start("1")
    handle_text("1", "стиральные машины")
    handle_text("1", "Стиральная машина Candy CS4")
    handle_photo("1", b"PHOTOBYTES")
    handle_text("1", "Тихая, экономичная, 4 кг")
    r = handle_callback("1", "wizard:confirm")
    assert "поставлено" in r.text
    assert calls[0][2] == "Тихая, экономичная, 4 кг"


def test_list_step_reports_unmatched_with_candidates(tmp_path):
    start, handle_text, *_ = _flow(tmp_path)
    start("1")
    handle_text("1", "стиральные машины")
    r = handle_text("1", "Стиральная машина Beko WSRE6512\nНечто совсем другое xyz123")
    assert "найдено 1 из 2" in r.text
    assert "не найдено" in r.text.lower()
    assert "xyz123" in r.text


def test_cancel_resets_wizard(tmp_path):
    start, handle_text, _, handle_callback, calls, store = _flow(tmp_path)
    start("1")
    handle_text("1", "стиральные машины")
    r = handle_callback("1", "wizard:cancel")
    assert "отменено" in r.text.lower()
    assert store.snapshot("1") is None


def test_confirm_with_nothing_matched(tmp_path):
    start, handle_text, _, handle_callback, calls, store = _flow(tmp_path)
    start("1")
    handle_text("1", "стиральные машины")
    handle_text("1", "Абсолютно несуществующий товар zzz")
    handle_callback("1", "wizard:skip_photo")
    r = handle_callback("1", "wizard:skip_utp")
    r = handle_callback("1", "wizard:confirm")
    assert "❌" in r.text
