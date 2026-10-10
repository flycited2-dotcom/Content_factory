"""Owner acceptance: supplier first, scoped tree, custom first N and ranges."""
import json

import pytest

from content_factory.bot.wizard import WizardStore
import content_factory.bot.wizard_flow as wf
from content_factory.ingest.excel_price import PriceItem


def buttons(reply):
    return [b for row in reply.markup["inline_keyboard"] for b in row]


def action(reply, prefix):
    return next(b["callback_data"] for b in buttons(reply)
                if b["callback_data"].startswith("wizard:" + prefix))


def button(reply, text):
    return next(b["callback_data"] for b in buttons(reply) if b["text"] == text)


def fixture(tmp_path, monkeypatch, total=45):
    prices = tmp_path / "prices"
    prices.mkdir()
    (prices / "source_names.json").write_text(json.dumps({
        "manual__быттехопт_auto": "БытТехОпт", "mail__mi": "Мир инструмента",
        "splithub": "СплитХаб", "aru": "АРУ"}), encoding="utf-8")
    aru = [PriceItem("Электроинструмент / Перфораторы", str(n), "АРУ",
                     f"Перфоратор АРУ М{n:03}", 100 + n,
                     ("Электроинструмент", "Перфораторы"),
                     ("/elektroinstrument/", "/elektroinstrument/perforatory/"))
           for n in range(total)]
    slots = [("manual__быттехопт_auto", [PriceItem("Холодильники", "b", "БТО", "Холодильник БТО B1", 10)]),
             ("mail__mi", [PriceItem("Электроинструмент / Перфораторы", "mi", "МИ", "Перфоратор МИ X1", 20,
                                    ("Электроинструмент", "Перфораторы"),
                                    ("/elektroinstrument/", "/elektroinstrument/perforatory/"))]),
             ("splithub", [PriceItem("Кондиционеры", "sh", "SH", "Кондиционер SH S1", 30)]),
             ("aru", aru)]
    monkeypatch.setattr(wf, "load_price_slots", lambda *args, **kwargs: slots)
    monkeypatch.setattr(wf, "top_sections", lambda *args, **kwargs: [i.section for _, rows in slots for i in rows])
    state = tmp_path / "state.db"
    store = WizardStore(state)
    flow = wf.make_wizard_flow(state, prices, store, lambda *a: 0,
                               lambda *a: "", lambda: "СТАТУС")
    return flow, store, slots, prices, state


def select_aru(flow):
    start, _, _, callback = flow
    suppliers = start("owner")
    categories = callback("owner", next(b["callback_data"] for b in buttons(suppliers)
                                         if b["text"].startswith("АРУ ·")))
    subgroups = callback("owner", button(categories, "Электроинструмент"))
    return callback("owner", button(subgroups, "Перфораторы"))


def test_task_first_step_is_suppliers_with_counts_and_russian_names(tmp_path, monkeypatch):
    flow, store, _, _, _ = fixture(tmp_path, monkeypatch)
    reply = flow[0]("owner")
    assert store.snapshot("owner").step == "awaiting_source"
    assert [b["text"] for b in buttons(reply) if b["callback_data"].startswith("wizard:source:")] == [
        "БытТехОпт · 1", "Мир инструмента · 1", "СплитХаб · 1", "АРУ · 45"]
    assert not any(b["callback_data"].startswith("wizard:cat:") for b in buttons(reply))


def test_selected_supplier_scopes_category_tree_and_product_prices(tmp_path, monkeypatch):
    flow, store, _, _, _ = fixture(tmp_path, monkeypatch)
    products = select_aru(flow)
    assert store.snapshot("owner").source_slot == "aru"
    assert len(store.snapshot("owner").candidates) == 45
    assert "МИ X1" not in products.text and "Холодильник" not in products.text
    assert "Электроинструмент / Перфораторы" in products.text


def test_source_is_persisted_across_restart_and_category_back(tmp_path, monkeypatch):
    flow, _, _, prices, state = fixture(tmp_path, monkeypatch)
    select_aru(flow)
    store = WizardStore(state)
    restarted = wf.make_wizard_flow(state, prices, store, lambda *a: 0, lambda *a: "", lambda: "СТАТУС")
    reply = restarted[3]("owner", "wizard:categories")
    assert store.snapshot("owner").source_slot == "aru"
    assert "Электроинструмент" in str(reply.markup)
    assert "Холодильники" not in str(reply.markup)
    assert "СплитХаб" not in str(reply.markup)


def test_old_supplier_category_button_cannot_select_current_supplier(tmp_path, monkeypatch):
    flow, store, _, _, _ = fixture(tmp_path, monkeypatch)
    start, _, _, callback = flow
    sources = start("owner")
    mi = callback("owner", next(b["callback_data"] for b in buttons(sources)
                                  if b["text"].startswith("Мир инструмента ·")))
    old = button(mi, "Электроинструмент")
    sources = callback("owner", "wizard:sources")
    callback("owner", next(b["callback_data"] for b in buttons(sources)
                             if b["text"].startswith("АРУ ·")))
    result = callback("owner", old)
    assert "устарел" in result.text
    assert store.snapshot("owner").source_slot == "aru"
    assert store.snapshot("owner").candidates is None


def test_category_text_requires_supplier_first_and_stays_scoped(tmp_path, monkeypatch):
    flow, store, _, _, _ = fixture(tmp_path, monkeypatch)
    flow[0]("owner")
    reply = flow[1]("owner", "перфораторы")
    assert "поставщика" in reply.text
    assert store.snapshot("owner").step == "awaiting_source"
    select_aru(flow)
    flow[3]("owner", "wizard:categories")
    reply = flow[1]("owner", "Перфоратор")
    assert len(store.snapshot("owner").candidates) == 45
    assert "МИ X1" not in reply.text


def test_custom_first_n_button_and_exact_prefix_after_restart(tmp_path, monkeypatch):
    flow, _, _, prices, state = fixture(tmp_path, monkeypatch)
    products = select_aru(flow)
    assert any(b["text"] == "✏️ Своё число" for b in buttons(products))
    reply = flow[3]("owner", button(products, "✏️ Своё число"))
    assert "от 1 до 45" in reply.text
    store = WizardStore(state)
    assert store.snapshot("owner").step == "awaiting_count"
    restarted = wf.make_wizard_flow(state, prices, store, lambda *a: 0, lambda *a: "", lambda: "СТАТУС")
    reply = restarted[1]("owner", "23")
    assert store.snapshot("owner").step == "awaiting_time"
    assert [c[2] for c in store.snapshot("owner").candidates] == [f"М{n:03}" for n in range(23)]
    assert "Сейчас" in str(reply.markup)


@pytest.mark.parametrize("invalid", ["0", "46", "-2", "2.5", "1 3", "все", "abc"])
def test_custom_count_rejects_noninteger_or_out_of_bounds(tmp_path, monkeypatch, invalid):
    flow, store, _, _, _ = fixture(tmp_path, monkeypatch)
    products = select_aru(flow)
    flow[3]("owner", button(products, "✏️ Своё число"))
    result = flow[1]("owner", invalid)
    assert store.snapshot("owner").step == "awaiting_count"
    assert len(store.snapshot("owner").candidates) == 45
    assert "от 1 до 45" in result.text


def test_numbered_selection_and_ranges_expand_and_deduplicate(tmp_path, monkeypatch):
    flow, store, _, _, _ = fixture(tmp_path, monkeypatch)
    select_aru(flow)
    flow[1]("owner", "3 14 32, 10-15, 14")
    assert store.snapshot("owner").step == "awaiting_time"
    assert [c[2] for c in store.snapshot("owner").candidates] == [
        "М002", "М013", "М031", "М009", "М010", "М011", "М012", "М014"]


@pytest.mark.parametrize("invalid", ["0 3", "44-46", "15-10", "abc3", "1.5"])
def test_invalid_numbered_selection_does_not_silently_shrink(tmp_path, monkeypatch, invalid):
    flow, store, _, _, _ = fixture(tmp_path, monkeypatch)
    select_aru(flow)
    reply = flow[1]("owner", invalid)
    assert store.snapshot("owner").step == "awaiting_pick"
    assert "1 3 5" in reply.text and "10-15" in reply.text


def test_exactly_ten_categories_per_page_and_all_reachable(tmp_path, monkeypatch):
    flow, _, slots, _, _ = fixture(tmp_path, monkeypatch)
    slots[3][1][:] = [PriceItem(f"Группа {n:02}", str(n), "АРУ", f"АРУ М{n:03}", 100)
                      for n in range(23)]
    suppliers = flow[0]("owner")
    reply = flow[3]("owner", next(b["callback_data"] for b in buttons(suppliers)
                                  if b["text"].startswith("АРУ ·")))
    pages = []
    for expected in (10, 10, 3):
        cats = [b["text"] for b in buttons(reply) if b["callback_data"].startswith("wizard:cat:")]
        assert len(cats) == expected
        pages.extend(cats)
        if len(pages) < 23:
            reply = flow[3]("owner", button(reply, "Ещё ▸"))
    assert len(set(pages)) == 23


def test_legacy_database_migrates_source_slot_without_losing_active_selection(tmp_path):
    import sqlite3
    state = tmp_path / "old.db"
    with sqlite3.connect(state) as db:
        db.execute("CREATE TABLE wizard_state(chat_id TEXT PRIMARY KEY,step TEXT,category TEXT,"
                   "lines_json TEXT,photo_path TEXT,utp_text TEXT,candidates_json TEXT,due_at REAL,ts REAL)")
        import time
        db.execute("INSERT INTO wizard_state(chat_id,step,candidates_json,ts) VALUES(?,?,?,?)",
                   ("owner", "awaiting_pick", json.dumps([["k", "B", "M", "Name", 100]]), time.time()))
    store = WizardStore(state)
    snapshot = store.snapshot("owner")
    assert snapshot.source_slot is None
    assert snapshot.candidates == [["k", "B", "M", "Name", 100]]


@pytest.mark.parametrize("total", [1, 5, 10])
def test_all_button_remains_available_for_small_supplier_group(tmp_path, monkeypatch, total):
    flow, store, _, _, _ = fixture(tmp_path, monkeypatch, total=total)
    products = select_aru(flow)
    flow[3]("owner", button(products, f"✅ Все {total}"))
    assert store.snapshot("owner").step == "awaiting_time"
    assert len(store.snapshot("owner").candidates) == total


def test_custom_count_back_keeps_current_page_and_checkbox_selection(tmp_path, monkeypatch):
    flow, store, _, _, _ = fixture(tmp_path, monkeypatch)
    products = select_aru(flow)
    products = flow[3]("owner", button(products, "▫️ 1"))
    products = flow[3]("owner", button(products, "Ещё ▸"))
    products = flow[3]("owner", button(products, "▫️ 21"))
    prompt = flow[3]("owner", button(products, "✏️ Своё число"))
    result = flow[3]("owner", button(prompt, "◀️ К списку товаров"))
    snapshot = store.snapshot("owner")
    assert snapshot.step == "awaiting_pick" and snapshot.page == 1
    assert len(snapshot.selected_keys) == 2
    assert "Выбрано: 2" in result.text


def test_source_label_edits_are_live_and_existing_callback_stays_valid(tmp_path, monkeypatch):
    flow, store, _, prices, _ = fixture(tmp_path, monkeypatch)
    sources = flow[0]("owner")
    old_button = next(b["callback_data"] for b in buttons(sources) if b["text"].startswith("АРУ ·"))
    labels = json.loads((prices / "source_names.json").read_text(encoding="utf-8"))
    labels['aru'] = 'Новый АРУ'
    (prices / "source_names.json").write_text(json.dumps(labels), encoding="utf-8")
    updated = flow[3]("owner", "wizard:sources")
    assert "Новый АРУ · 45" in [b["text"] for b in buttons(updated)]
    categories = flow[3]("owner", old_button)
    assert "Новый АРУ" in categories.text
    assert store.snapshot("owner").source_slot == "aru"


def test_supplier_and_parent_counts_deduplicate_existing_identity_per_source(tmp_path, monkeypatch):
    from dataclasses import replace
    flow, store, slots, _, _ = fixture(tmp_path, monkeypatch)
    # Same selectable model can occur in two sections of one supplier.
    original = slots[3][1][0]
    slots[3][1].append(replace(original, section="Электроинструмент / Другие инструменты",
                              category_path=("Электроинструмент", "Другие инструменты"),
                              category_ids=("/elektroinstrument/", "/elektroinstrument/other/")))
    # Another supplier's same model must keep its own source count.
    slots[1][1].append(original)
    sources = flow[0]("owner")
    assert "АРУ · 45" in [b["text"] for b in buttons(sources)]
    assert "Мир инструмента · 2" in [b["text"] for b in buttons(sources)]
    categories = flow[3]("owner", button(sources, "АРУ · 45"))
    group = flow[3]("owner", button(categories, "Электроинструмент"))
    assert "Товаров в наличии: 45" in group.text
    assert any(b["text"] == "📋 Товары всей группы · 45" for b in buttons(group))
    flow[3]("owner", action(group, "catitems:"))
    assert len(store.snapshot("owner").candidates) == 45
