"""ARU articles sharing a research model remain independently selectable."""
from datetime import datetime, timezone
from dataclasses import replace
import json
import pytest

from content_factory.ingest.aru_account import build_account_snapshot, load_account_items
from content_factory.ingest.excel_price import PriceItem, item_key, search_items, match_model_lines
from content_factory.orchestrator.excel_pipeline import ExcelStore, _cache_key
from content_factory.bot.wizard import WizardStore
import content_factory.bot.wizard_flow as wf


def buttons(reply):
    return [b for row in reply.markup["inline_keyboard"] for b in row]


def button(reply, text):
    return next(b["callback_data"] for b in buttons(reply) if b["text"] == text)


def fixture(tmp_path, monkeypatch, total=2):
    prices = tmp_path / "prices"
    prices.mkdir()
    (prices / "source_names.json").write_text(json.dumps({"aru": "АРУ"}), encoding="utf-8")
    slots = [("other1", []), ("other2", []), ("other3", []), ("aru", [])]
    monkeypatch.setattr(wf, "load_price_slots", lambda *a, **k: slots)
    monkeypatch.setattr(wf, "top_sections", lambda *a, **k: [i.section for _, rows in slots for i in rows])
    state = tmp_path / "state.db"
    store = WizardStore(state)
    flow = wf.make_wizard_flow(state, prices, store, lambda *a: 0, lambda *a: "", lambda: "СТАТУС")
    return flow, store, slots, prices, state


def select_aru(flow):
    sources = flow[0]("owner")
    categories = flow[3]("owner", next(b["callback_data"] for b in buttons(sources) if b["text"].startswith("АРУ ·")))
    groups = flow[3]("owner", button(categories, "Электроинструмент"))
    return flow[3]("owner", button(groups, "Перфораторы"))


def loaded(tmp_path):
    rows = [dict(id=str(n), article=f"A{n}", brand="TOOL",
                 name=f"Перфоратор TOOL M1 (комплект {n})",
                 url=f"https://aru.ooo/elektroinstrument/perforatory/m1-{n}/",
                 stock_text="В наличии", price_text="100,01 ₽") for n in (100, 101)]
    capture = dict(page=1, pages=1, url="https://aru.ooo/vse-tovary/",
                   authenticated=True, captured_at=datetime.now(timezone.utc).isoformat(), items=rows)
    snapshot = build_account_snapshot([capture])
    (tmp_path / "aru-catalog.json").write_text(json.dumps(snapshot), encoding="utf-8")
    return load_account_items(tmp_path)


def test_loader_uses_supplier_ids_without_changing_price_or_research_model(tmp_path):
    items = loaded(tmp_path)
    assert [item_key(i) for i in items] == ["excel|aru:100", "excel|aru:101"]
    assert [i.price for i in items] == [111, 111]
    store = ExcelStore(tmp_path / "state.db")
    rows = [(item_key(i), i.brand, "M1", i.name, i.price) for i in items]
    assert len(store.select_items(rows)) == 2
    assert store.get(rows[0][0]) is not None and store.get(rows[1][0]) is not None
    assert _cache_key(store.get(rows[0][0])) == _cache_key(store.get(rows[1][0])) == "tool|m1"


def test_search_keeps_variants_and_id_specific_taken_blocks_only_one(tmp_path):
    items = loaded(tmp_path)
    assert len(search_items(items, "Перфоратор", set())) == 2
    remaining = search_items(items, "Перфоратор", {"excel|aru:100"})
    assert [item_key(i) for i in remaining] == ["excel|aru:101"]


def test_legacy_taken_model_still_blocks_all_previously_selected_variants(tmp_path):
    items = loaded(tmp_path)
    old = "excel|tool|m1"
    assert not search_items(items, "Перфоратор", {old})
    assert not match_model_lines(items, ["Перфоратор TOOL M1"], {old})[0].candidates
    assert match_model_lines(items, ["Перфоратор TOOL M1"], {old})[0].item is None


def test_other_supplier_and_old_serialized_price_items_keep_existing_key():
    item = PriceItem("Инструмент", "A", "TOOL", "Перфоратор TOOL M1 (комплект)", 111)
    assert item_key(item) == "excel|tool|m1"
    assert item_key(PriceItem(**item.__dict__)) == item_key(item)


def test_supplier_tree_checkboxes_and_first_n_do_not_collapse_variants(tmp_path, monkeypatch):
    flow, store, slots, _, _ = fixture(tmp_path, monkeypatch, total=2)
    items = loaded(tmp_path)
    slots[3][1][:] = [replace(item, section="Электроинструмент / Перфораторы",
        category_path=("Электроинструмент", "Перфораторы"),
        category_ids=("/elektroinstrument/", "/elektroinstrument/perforatory/")) for item in items]
    sources = flow[0]("owner")
    assert "АРУ · 2" in [b["text"] for b in buttons(sources)]
    products = select_aru(flow)
    assert len(store.snapshot("owner").candidates) == 2
    products = flow[3]("owner", button(products, "▫️ 1"))
    products = flow[3]("owner", button(products, "▫️ 2"))
    assert set(store.snapshot("owner").selected_keys) == {"excel|aru:100", "excel|aru:101"}
    flow[3]("owner", button(products, "✏️ Своё число"))
    flow[1]("owner", "2")
    assert {row[0] for row in store.snapshot("owner").candidates} == {"excel|aru:100", "excel|aru:101"}


@pytest.mark.parametrize("legacy_task_appears", [False, True])
def test_confirmation_preserves_variants_and_rechecks_historical_tasks(tmp_path, monkeypatch, legacy_task_appears):
    flow, store, slots, _, state = fixture(tmp_path, monkeypatch)
    items = loaded(tmp_path)
    slots[3][1][:] = [replace(item, section="Электроинструмент / Перфораторы",
        category_path=("Электроинструмент", "Перфораторы"),
        category_ids=("/elektroinstrument/", "/elektroinstrument/perforatory/")) for item in items]
    select_aru(flow)
    flow[3]("owner", "wizard:pick_first:2")
    flow[3]("owner", "wizard:time_now")
    flow[3]("owner", "wizard:skip_photo")
    flow[3]("owner", "wizard:skip_utp")
    excel = ExcelStore(state)
    if legacy_task_appears:
        excel.add_items([("excel|tool|m1", "TOOL", "M1", items[0].name, 111)])
    flow[3]("owner", "wizard:confirm")
    expected = {"excel|tool|m1"} if legacy_task_appears else {"excel|aru:100", "excel|aru:101"}
    assert {row.key for row in excel.by_status("new")} == expected
    flow[3]("owner", "wizard:confirm")
    assert {row.key for row in excel.by_status("new")} == expected
