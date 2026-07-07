"""Оркестрация визарда /task v2: категория (кнопки/текст) → автосписок с
номерами (или свой список строк) → время («сейчас»/расписание) → (для «сейчас»)
опц. фото → опц. УТП → подтверждение. Чистая логика (без Telegram)."""
import time as _time
from datetime import datetime

import openpyxl
from content_factory.bot.wizard import WizardStore
from content_factory.bot.wizard_flow import make_wizard_flow
from content_factory.orchestrator.excel_pipeline import ExcelStore

NOW = datetime(2026, 7, 7, 12, 0)


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
    ["Телевизоры", "", "", "", "", ""],
    ["3", "20", "MIU", "Телевизор MIU H32 Smart", "8650", ""],
]


def _flow(tmp_path, submit_card=None):
    prices = _price(tmp_path, ROWS)
    store = WizardStore(tmp_path / "wizard.db")

    def save_photo(chat_id, photo_bytes):
        p = tmp_path / f"photo_{chat_id}.jpg"
        p.write_bytes(photo_bytes)
        return str(p)

    calls = []

    def _submit_card(brand, model, utp, photo_path):
        calls.append((brand, model, utp, photo_path))
        return submit_card(brand, model, utp, photo_path) if submit_card else 999

    start, handle_text, handle_photo, handle_callback = make_wizard_flow(
        tmp_path / "state.db", prices, store, _submit_card, save_photo,
        excel_fn=lambda: "СТАТУС", now_fn=lambda: NOW)
    return start, handle_text, handle_photo, handle_callback, calls, store


def test_start_offers_category_buttons(tmp_path):
    start, *_ = _flow(tmp_path)
    r = start("1")
    flat = [b for row in r.markup["inline_keyboard"] for b in row]
    cats = [b for b in flat if b["callback_data"].startswith("wizard:cat:")]
    assert len(cats) == 2                              # два раздела из прайса
    assert any("Стиральные машины" in b["text"] for b in cats)


def test_category_button_returns_numbered_autolist(tmp_path):
    start, _, _, handle_callback, _, store = _flow(tmp_path)
    start("1")
    sections_btn_idx = 0                               # топ-раздел: Стиральные машины
    r = handle_callback("1", f"wizard:cat:{sections_btn_idx}")
    assert "1." in r.text and "2." in r.text           # нумерованный список
    assert store.snapshot("1").step == "awaiting_pick"


def test_category_text_also_works(tmp_path):
    start, handle_text, *_ = _flow(tmp_path)
    start("1")
    r = handle_text("1", "телевизоры")
    assert "MIU" in r.text and "1." in r.text


# ── пагинация категорий: групп бывает 200+ (Аксёнов+БытТехОпт), в одно
# сообщение все кнопки не влезают — листаем по страницам ──────────────────────
def test_category_buttons_paginated_when_many(tmp_path, monkeypatch):
    import content_factory.bot.wizard_flow as wf
    many = [f"Группа {i:03d}" for i in range(60)]
    monkeypatch.setattr(wf, "top_sections", lambda pd: many)
    kb = wf._category_keyboard(tmp_path, page=0)
    flat = [b for row in kb["inline_keyboard"] for b in row]
    cats = [b for b in flat if b["callback_data"].startswith("wizard:cat:")]
    assert len(cats) == wf._CATS_PER_PAGE                 # первая страница
    assert cats[0]["callback_data"] == "wizard:cat:0"
    assert any(b["callback_data"] == "wizard:catpage:1" for b in flat)   # «▸»
    assert not any(b["callback_data"] == "wizard:catpage:-1" for b in flat)

    kb2 = wf._category_keyboard(tmp_path, page=1)
    flat2 = [b for row in kb2["inline_keyboard"] for b in row]
    cats2 = [b for b in flat2 if b["callback_data"].startswith("wizard:cat:")]
    # индексы ГЛОБАЛЬНЫЕ (резолв по top_sections), страница 2 начинается с 24
    assert cats2[0]["callback_data"] == f"wizard:cat:{wf._CATS_PER_PAGE}"
    assert any(b["callback_data"] == "wizard:catpage:0" for b in flat2)  # «◂»


def test_catpage_callback_flips_page(tmp_path, monkeypatch):
    import content_factory.bot.wizard_flow as wf
    many = [f"Группа {i:03d}" for i in range(60)]
    monkeypatch.setattr(wf, "top_sections", lambda pd: many)
    start, _, _, handle_callback, _, store = _flow(tmp_path)
    start("1")
    r = handle_callback("1", "wizard:catpage:2")
    flat = [b for row in r.markup["inline_keyboard"] for b in row]
    cats = [b for b in flat if b["callback_data"].startswith("wizard:cat:")]
    assert cats[0]["callback_data"] == f"wizard:cat:{2 * wf._CATS_PER_PAGE}"
    assert store.snapshot("1").step == "awaiting_category"   # шаг не сломан


def test_pick_numbers_then_now_then_confirm(tmp_path):
    start, handle_text, _, handle_callback, calls, store = _flow(tmp_path)
    start("1")
    handle_text("1", "стиральные машины")
    r = handle_text("1", "2")                          # выбрали Candy
    assert "Сейчас" in str(r.markup)                   # шаг времени
    r = handle_callback("1", "wizard:time_now")
    assert "фото" in r.text.lower()
    handle_callback("1", "wizard:skip_photo")
    r = handle_callback("1", "wizard:skip_utp")
    assert "Подтвердить" in r.text and "сейчас" in r.text
    r = handle_callback("1", "wizard:confirm")
    assert "поставлено" in r.text

    es = ExcelStore(tmp_path / "state.db")
    items = es.by_status("new")
    assert [i.name for i in items] == ["Стиральная машина Candy CS4"]
    assert calls == []                                 # без фото — обычный конвейер


def test_pick_all_keyword(tmp_path):
    start, handle_text, _, handle_callback, _, store = _flow(tmp_path)
    start("1")
    handle_text("1", "стиральные машины")
    handle_text("1", "все")
    assert len(store.snapshot("1").candidates) == 2


def test_scheduled_time_skips_photo_and_sets_due_at(tmp_path):
    start, handle_text, _, handle_callback, calls, store = _flow(tmp_path)
    start("1")
    handle_text("1", "телевизоры")
    handle_text("1", "1")
    r = handle_text("1", "завтра 9:00")                # расписание текстом
    assert "Подтвердить" in r.text and "08.07 09:00" in r.text   # фото/УТП пропущены
    r = handle_callback("1", "wizard:confirm")
    assert "запланировано" in r.text

    es = ExcelStore(tmp_path / "state.db")
    assert es.by_status("new") == []                   # до срока тик не видит
    import sqlite3
    with sqlite3.connect(tmp_path / "state.db") as c:
        due = c.execute("SELECT due_at FROM excel_items").fetchone()[0]
    assert due == datetime(2026, 7, 8, 9, 0).timestamp()


def test_manual_multiline_list_still_works(tmp_path):
    start, handle_text, handle_photo, handle_callback, calls, store = _flow(tmp_path)
    start("1")
    r = handle_text("1", "Стиральная машина Beko WSRE6512\nТелевизор MIU H32 Smart")
    assert "найдено 2 из 2" in r.text
    handle_callback("1", "wizard:time_now")
    handle_photo("1", b"PHOTOBYTES")                   # фото-override при «сейчас»
    handle_callback("1", "wizard:skip_utp")
    r = handle_callback("1", "wizard:confirm")
    assert "минуя research" in r.text
    assert len(calls) == 2                             # submit_card на обе позиции
    es = ExcelStore(tmp_path / "state.db")
    assert len(es.by_status("card")) == 2


def test_bad_time_reasks(tmp_path):
    start, handle_text, *_ = _flow(tmp_path)
    start("1")
    handle_text("1", "телевизоры")
    handle_text("1", "1")
    r = handle_text("1", "когда-нибудь потом")
    assert "❌" in r.text and "Сейчас" in str(r.markup)


def test_status_callback_does_not_disrupt(tmp_path):
    start, handle_text, _, handle_callback, _, store = _flow(tmp_path)
    start("1")
    handle_text("1", "телевизоры")
    r = handle_callback("1", "wizard:status")
    assert r.text == "СТАТУС"
    assert store.snapshot("1").step == "awaiting_pick"   # диалог не сброшен


def test_cancel_any_step(tmp_path):
    start, handle_text, _, handle_callback, _, store = _flow(tmp_path)
    start("1")
    handle_text("1", "телевизоры")
    r = handle_callback("1", "wizard:cancel")
    assert "отменено" in r.text.lower()
    assert store.snapshot("1") is None
