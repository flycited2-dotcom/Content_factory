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
    # часы инжектим (NOW), иначе тест ломается, как только реальный день > due
    assert es.by_status("new", now=NOW.timestamp()) == []   # до срока тик не видит
    assert len(es.by_status("new", now=NOW.timestamp() + 86400)) == 1  # после срока — виден
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


def test_commands_pass_through_wizard(tmp_path):
    # ГРАБЛЯ 2026-07-09: бот залип в awaiting_pick и жрал /auto /status /make —
    # команды должны проходить СКВОЗЬ визард (не сбрасывая диалог)
    start, handle_text, _, handle_callback, _, store = _flow(tmp_path)
    start("1")
    handle_text("1", "телевизоры")
    assert handle_text("1", "/auto") is None
    assert handle_text("1", "/status") is None
    assert store.snapshot("1").step == "awaiting_pick"     # диалог не сброшен


def test_autolist_and_bad_pick_have_cancel_button(tmp_path):
    # выход из залипшего шага — кнопкой, а не магическим словом
    start, handle_text, _, _, _, store = _flow(tmp_path)
    start("1")
    r = handle_text("1", "телевизоры")
    assert "wizard:cancel" in str(r.markup)
    r = handle_text("1", "ерунда без номеров")
    assert "wizard:cancel" in str(r.markup)


def test_manual_product_full_path(tmp_path):
    # п.4 (2026-07-09): свой товар НЕ из прайсов/остатков — вручную в генерацию
    start, handle_text, _, handle_callback, calls, store = _flow(tmp_path)
    r = start("1")
    assert "wizard:manual" in str(r.markup)                # кнопка «Свой товар»
    r = handle_callback("1", "wizard:manual")
    assert "азвание" in r.text                             # просит название
    r = handle_text("1", "Кондиционер BORK AC-3001 белый")
    assert "цен" in r.text.lower()                         # просит цену
    r = handle_text("1", "45 990")
    assert "Сейчас" in str(r.markup)                       # дальше стандартный шаг времени
    st = store.snapshot("1")
    assert st.step == "awaiting_time"
    (key, brand, model, name, price), = st.candidates
    assert key.startswith("manual|") and price == 45990
    assert name == "Кондиционер BORK AC-3001 белый"
    # довести до конца: сейчас → без фото → без УТП → подтвердить
    handle_callback("1", "wizard:time_now")
    handle_callback("1", "wizard:skip_photo")
    handle_callback("1", "wizard:skip_utp")
    r = handle_callback("1", "wizard:confirm")
    assert "поставлено" in r.text
    es = ExcelStore(tmp_path / "state.db")
    assert [i.name for i in es.by_status("new")] == ["Кондиционер BORK AC-3001 белый"]


def test_manual_product_bad_price_reasks(tmp_path):
    start, handle_text, _, handle_callback, _, store = _flow(tmp_path)
    start("1")
    handle_callback("1", "wizard:manual")
    handle_text("1", "Тепловая пушка Ballu BHP-5")
    r = handle_text("1", "дорого")
    assert "❌" in r.text                                  # не число — переспросить
    assert store.snapshot("1").step == "awaiting_manual_price"


def test_manual_button_works_from_any_step(tmp_path):
    # «сразу летит ошибка» (2026-07-09): кнопка «Свой товар» жалась на шаге
    # списка → «неожиданное действие»; теперь стартует ветку с любого шага
    start, handle_text, _, handle_callback, _, store = _flow(tmp_path)
    start("1")
    handle_text("1", "телевизоры")                        # шаг awaiting_pick
    r = handle_callback("1", "wizard:manual")
    assert "азвание" in r.text                            # просит название, не ошибку
    assert store.snapshot("1").step == "awaiting_manual_name"


def test_manual_prompts_use_force_reply(tmp_path):
    # UX 2026-07-09: «нажимаю Свой товар и вылазит снова то же» — запрос названия
    # выглядел как ошибка (❌ Отмена); теперь force_reply открывает поле ввода
    start, handle_text, _, handle_callback, _, store = _flow(tmp_path)
    start("1")
    r = handle_callback("1", "wizard:manual")
    assert r.markup.get("force_reply") is True
    assert "BORK" in r.markup.get("input_field_placeholder", "")
    assert "ответным сообщением" in r.text                # прямая инструкция
    r = handle_text("1", "Кондиционер BORK AC-3001")
    assert r.markup.get("force_reply") is True            # и на шаге цены
    assert r.markup.get("input_field_placeholder") == "45990"


def test_confirm_resolves_relative_photo_path(tmp_path, monkeypatch):
    # ГРАБЛЯ 2026-07-09 (crash-loop cf-bot, 31 рестарт): photo_path хранился
    # относительным, card_submit клеил его с output_dir агента → FileNotFoundError
    start, handle_text, _, handle_callback, calls, store = _flow(tmp_path)
    start("1")
    handle_text("1", "телевизоры")
    handle_text("1", "1")
    handle_callback("1", "wizard:time_now")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rel.jpg").write_bytes(b"IMG")
    store.set_photo("1", "rel.jpg")                       # относительный путь
    handle_callback("1", "wizard:skip_utp")
    r = handle_callback("1", "wizard:confirm")
    assert "поставлено" in r.text
    assert calls and str(tmp_path / "rel.jpg") == calls[0][3]   # абсолютизирован


def test_confirm_missing_photo_reasks_instead_of_crash(tmp_path):
    start, handle_text, _, handle_callback, calls, store = _flow(tmp_path)
    start("1")
    handle_text("1", "телевизоры")
    handle_text("1", "1")
    handle_callback("1", "wizard:time_now")
    store.set_photo("1", str(tmp_path / "нет_такого.jpg"))
    handle_callback("1", "wizard:skip_utp")
    r = handle_callback("1", "wizard:confirm")
    assert "фото" in r.text.lower() and "заново" in r.text      # мягкий ответ
    assert calls == []                                    # submit_card не дёргали
    assert store.snapshot("1").step == "awaiting_photo"   # вернулись на шаг фото


def test_confirm_offers_redo_photo_and_utp(tmp_path):
    # «кнопка назад» (2026-07-09): на подтверждении можно переснять фото/УТП
    start, handle_text, handle_photo, handle_callback, _, store = _flow(tmp_path)
    start("1")
    handle_text("1", "телевизоры")
    handle_text("1", "1")
    handle_callback("1", "wizard:time_now")
    handle_photo("1", b"OLD")
    r = handle_callback("1", "wizard:skip_utp")
    assert "wizard:redo_photo" in str(r.markup) and "wizard:redo_utp" in str(r.markup)

    r = handle_callback("1", "wizard:redo_photo")          # назад к фото
    assert store.snapshot("1").step == "awaiting_photo"
    handle_photo("1", b"NEW")                              # новое заменяет старое
    assert (tmp_path / "photo_1.jpg").read_bytes() == b"NEW"

    handle_callback("1", "wizard:skip_utp")
    r = handle_callback("1", "wizard:redo_utp")            # назад к УТП
    assert store.snapshot("1").step == "awaiting_utp"
    handle_text("1", "Тихий, экономичный")
    assert store.snapshot("1").utp_text == "Тихий, экономичный"


def test_confirm_submit_failure_keeps_item_out_of_research(tmp_path):
    # ГРАБЛЯ 2026-07-09: add_items шёл ДО submit_card; падение сабмита оставляло
    # товар в status=new → excel-тик гнал его в research с ЧУЖИМ фото ChatGPT
    def boom(brand, model, utp, photo):
        raise RuntimeError("api down")
    start, handle_text, handle_photo, handle_callback, calls, store = _flow(
        tmp_path, submit_card=None)
    # подменяем submit на падающий через обвязку _flow нельзя — собираем заново
    from content_factory.bot.wizard import WizardStore
    from content_factory.bot.wizard_flow import make_wizard_flow
    from content_factory.orchestrator.excel_pipeline import ExcelStore
    prices = tmp_path / "prices"                    # прайс уже создан _flow
    store2 = WizardStore(tmp_path / "wizard2.db")
    start2, htext2, hphoto2, hcb2 = make_wizard_flow(
        tmp_path / "state2.db", prices, store2, boom,
        lambda cid, b: str(tmp_path / "p.jpg"), excel_fn=lambda: "S")
    (tmp_path / "p.jpg").write_bytes(b"IMG")
    start2("1")
    htext2("1", "телевизоры")
    htext2("1", "1")
    hcb2("1", "wizard:time_now")
    hphoto2("1", b"IMG")
    hcb2("1", "wizard:skip_utp")
    import pytest as _pytest
    with _pytest.raises(RuntimeError):
        hcb2("1", "wizard:confirm")                # _wizard_safe ловит выше, в боте
    es = ExcelStore(tmp_path / "state2.db")
    assert es.by_status("new") == []               # товар НЕ утёк в research-путь
