"""Расписание /task: владелец задаёт время ГОТОВНОСТИ фото, а тик конвейера
берёт товар только когда due_at дозрел — без упреждения генерация СТАРТУЕТ
в заданный час (инцидент 2026-07-10: «к 9:00» начало генериться в 9:00, фото
приехали к 10). Пишем в excel_items срок с упреждением + говорим оба времени.

Плюс регресс перегенерации: manual|-товары (ручные /make) не возвращались в
конвейер — regen_fn сбрасывал в 'new' только ключи excel| (грабля как 2026-07-06,
но для другого префикса; «Перегенерировать карточку» молчал НАВСЕГДА)."""
import sqlite3
from datetime import datetime
from types import SimpleNamespace

from content_factory.bot.run import make_regen_fn
from content_factory.bot.wizard_flow import (
    make_wizard_flow, TASK_LEAD_SECONDS, _LEAD_PER_ITEM_SECONDS)
from content_factory.orchestrator.excel_pipeline import ExcelStore

from tests.test_wizard_flow import _flow, NOW


def _scheduled_confirm(tmp_path):
    """Провести визард по пути «категория → 1 позиция → завтра 9:00 → confirm»."""
    start, handle_text, _, handle_callback, _, _ = _flow(tmp_path)
    start("1")
    handle_text("1", "телевизоры")                 # автосписок: 1 позиция (MIU)
    handle_text("1", "1")                          # взять №1
    handle_text("1", "завтра 9:00")                # расписание
    return handle_callback("1", "wizard:confirm")


def test_scheduled_due_at_written_with_lead(tmp_path):
    _scheduled_confirm(tmp_path)
    due_owner = datetime(2026, 7, 8, 9, 0).timestamp()      # NOW=07.07 12:00
    lead = max(TASK_LEAD_SECONDS, _LEAD_PER_ITEM_SECONDS * 1)
    rows = sqlite3.connect(tmp_path / "state.db").execute(
        "SELECT due_at FROM excel_items").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == due_owner - lead


def test_scheduled_confirm_message_mentions_both_times(tmp_path):
    r = _scheduled_confirm(tmp_path)
    assert "фото к 08.07 09:00" in r.text
    assert "генерация с 08.07 08:00" in r.text     # lead = 1 час на 1 позицию


def test_now_path_has_no_due_at(tmp_path):
    start, handle_text, _, handle_callback, _, _ = _flow(tmp_path)
    start("1")
    handle_text("1", "телевизоры")
    handle_text("1", "1")
    handle_callback("1", "wizard:time_now")
    handle_callback("1", "wizard:skip_photo")
    handle_callback("1", "wizard:skip_utp")
    handle_callback("1", "wizard:confirm")
    rows = sqlite3.connect(tmp_path / "state.db").execute(
        "SELECT due_at FROM excel_items").fetchall()
    assert rows and rows[0][0] is None


def test_regen_resets_manual_item_to_new(tmp_path):
    """Ключи manual|* (ручные /make) тоже должны возвращаться в 'new' —
    иначе «Перегенерировать карточку» для них молчит навсегда."""
    state_db = tmp_path / "state.db"
    store = ExcelStore(state_db)
    store.add_items([("manual|морозильный ларь hyundai ch1002", "Hyundai",
                      "CH1002", "Морозильный ларь Hyundai CH1002", 15990)])
    store.update("manual|морозильный ларь hyundai ch1002", status="preview")
    card = tmp_path / "card.png"
    card.write_bytes(b"png")
    regen = make_regen_fn(tmp_path / "card_jobs.db", state_db)
    a = SimpleNamespace(key="manual|морозильный ларь hyundai ch1002",
                        card_path=str(card))
    assert regen(a) is True
    status, due = sqlite3.connect(state_db).execute(
        "SELECT status, due_at FROM excel_items").fetchone()
    assert status == "new"
    assert not card.exists()


def test_manual_item_with_own_utp_fills_preview_cache(tmp_path):
    """Свой товар с СВОИМ фото и УТП: УТП уходил только агенту (на карточку),
    а research_cache оставался пуст → превью строило «Ключевые особенности»
    из кэша и подпись выходила ПУСТОЙ (2026-07-10, сплит Daicond)."""
    start, handle_text, handle_photo, handle_callback, calls, _ = _flow(tmp_path)
    start("1")
    handle_callback("1", "wizard:manual")
    handle_text("1", "Сплит-система Daicond серии ODYS, DN-OS09NW")
    handle_text("1", "12550")
    handle_callback("1", "wizard:time_now")
    handle_photo("1", b"IMG")
    handle_text("1", "✓ Wi-Fi\n✓ Режим TURBO")          # свой УТП
    handle_callback("1", "wizard:confirm")
    assert calls, "карточка должна уйти агенту (фото-override)"

    es = ExcelStore(tmp_path / "state.db")
    cached = es.cache_get("|сплит-система daicond серии odys, dn-os09nw")
    assert cached is not None, "УТП владельца должен лечь в research_cache"
    assert "TURBO" in cached[0]


def test_regen_works_after_reject(tmp_path):
    """«Отклонить» не должен хоронить перегенерацию: 🔄 по отклонённому превью
    обязан вернуть товар в конвейер (кейс ларя Hyundai, 2026-07-10)."""
    from content_factory.bot import commands as cmds
    from content_factory.orchestrator.confirm_store import ConfirmStore
    cs = ConfirmStore(tmp_path / "s.db")
    key = "manual|морозильный ларь hyundai ch1002"
    cs.add(key, "@chan", str(tmp_path / "card.png"), "cap")
    cs.mark(key, "rejected")

    regen_calls = []
    out = cmds.handle_command(f"/regen {key}", queue=None,
                              confirm_store=cs, regen_fn=lambda a: regen_calls.append(a.key))
    assert "перегенерации" in out
    assert regen_calls == [key]
    assert cs.get(key).status == "regen"
