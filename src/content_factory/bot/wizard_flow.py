"""Оркестрация визарда /task: категория → список моделей → (опц.) фото → (опц.)
УТП → подтверждение. Замена /make для владельца — не нужно помнить синтаксис
«число первым словом», список моделей сопоставляется построчно (match_model_lines).

Override-механика (см. план 2026-07-04-bot-task-wizard): submit_card (card_submit.py)
всегда требует готовое фото — поэтому «в обход research» можно поставить карточку,
ТОЛЬКО если владелец дал фото. УТП без фото — не даёт пропустить research (нечего
слать агенту), товар в этом случае идёт обычным путём (research переопределит УТП).
Если фото дано — УТП (если тоже дано) используется как есть, иначе пустая строка.

Чистая логика без Telegram: download/send инъецируются извне (bot/run.py)."""
from __future__ import annotations
from dataclasses import dataclass

from content_factory.ingest.excel_price import (
    load_price_slots, match_model_lines, item_key, extract_model)
from content_factory.orchestrator.excel_pipeline import ExcelStore
from content_factory.orchestrator.confirm_store import ConfirmStore
from content_factory.publish.telegram import PublishState

_SKIP_PHOTO_KB = {"inline_keyboard": [[
    {"text": "⏭ Пропустить", "callback_data": "wizard:skip_photo"}]]}
_SKIP_UTP_KB = {"inline_keyboard": [[
    {"text": "⏭ Пропустить", "callback_data": "wizard:skip_utp"}]]}
_CONFIRM_KB = {"inline_keyboard": [[
    {"text": "✅ Подтвердить", "callback_data": "wizard:confirm"},
    {"text": "❌ Отмена", "callback_data": "wizard:cancel"}]]}
_STATUS_KB = {"inline_keyboard": [[
    {"text": "📊 Статус", "callback_data": "wizard:status"}]]}


@dataclass
class WizardReply:
    text: str
    markup: dict | None = None


def make_wizard_flow(state_db, prices_dir, store, submit_card, save_photo, excel_fn):
    """submit_card(brand, model, utp, photo_path) -> job_id (см. card_submit.py).
    save_photo(chat_id, photo_bytes) -> абсолютный путь к сохранённому файлу.
    excel_fn() -> str — статус конвейера прайса (тот же текст, что /excel; кнопка
    «📊 Статус» работает независимо от активного диалога и не сбрасывает его)."""

    def _taken(excel_store: ExcelStore) -> set:
        return (PublishState(state_db).published_keys()
                | ConfirmStore(state_db).blocked_keys() | excel_store.all_keys())

    def _price_items():
        slots = load_price_slots(prices_dir)
        return [i for _, its in slots for i in its]

    def start(chat_id: str) -> WizardReply:
        store.start(chat_id)
        return WizardReply("🧾 Какая категория товара? Напишите текстом "
                           "(напр.: стиральные машины).", _STATUS_KB)

    def _confirm_prompt(st) -> WizardReply:
        photo = "есть" if st.photo_path else "нет"
        utp = "есть" if st.utp_text else "нет"
        return WizardReply(
            f"Категория: {st.category}\nПозиций в списке: {len(st.lines or [])}\n"
            f"Фото: {photo} · УТП: {utp}\n\nПодтвердить постановку в очередь?",
            _CONFIRM_KB)

    def handle_text(chat_id: str, text: str) -> WizardReply | None:
        st = store.snapshot(chat_id)
        if st is None:
            return None                                    # не в мастере — пусть идёт в handle_command
        text = (text or "").strip()

        if st.step == "awaiting_category":
            if not text:
                return WizardReply("❌ категория пустая — напишите текстом")
            store.set_category(chat_id, text)
            return WizardReply("📋 Пришлите список моделей — каждая с новой строки.")

        if st.step == "awaiting_list":
            lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
            if not lines:
                return WizardReply("❌ список пустой — пришлите хотя бы одну строку")
            excel_store = ExcelStore(state_db)
            matches = match_model_lines(_price_items(), lines, _taken(excel_store))
            store.set_list(chat_id, lines)
            found = [m for m in matches if m.item]
            missing = [m for m in matches if not m.item]
            lines_out = [f"✅ найдено {len(found)} из {len(matches)}:"]
            lines_out += [f"— {m.item.name} · {m.item.price:,} ₽".replace(",", " ")
                         for m in found]
            if missing:
                lines_out.append(f"\n❌ не найдено ({len(missing)}):")
                for m in missing:
                    cand = ", ".join(c.name[:40] for c in m.candidates) or "нет похожих"
                    lines_out.append(f"— «{m.line[:50]}» (похоже: {cand})")
            lines_out.append("\n📎 Пришлите фото (одно, на все позиции списка) "
                             "или пропустите.")
            return WizardReply("\n".join(lines_out), _SKIP_PHOTO_KB)

        if st.step == "awaiting_utp":
            store.set_utp(chat_id, text or None)
            return _confirm_prompt(store.snapshot(chat_id))

        return WizardReply("❌ сейчас жду не текст — см. предыдущее сообщение")

    def handle_photo(chat_id: str, photo_bytes: bytes) -> WizardReply | None:
        st = store.snapshot(chat_id)
        if st is None or st.step != "awaiting_photo":
            return None
        path = save_photo(chat_id, photo_bytes)
        store.set_photo(chat_id, path)
        return WizardReply("📝 Пришлите текст УТП или пропустите.", _SKIP_UTP_KB)

    def _do_confirm(chat_id: str, st) -> WizardReply:
        excel_store = ExcelStore(state_db)
        matches = match_model_lines(_price_items(), st.lines or [], _taken(excel_store))
        found = [m.item for m in matches if m.item]
        if not found:
            store.cancel(chat_id)
            return WizardReply("❌ ни одна позиция не подтвердилась "
                               "(возможно, уже в работе)")
        rows = [(item_key(i), i.brand, extract_model(i.name, i.brand), i.name, i.price)
               for i in found]
        excel_store.add_items(rows)
        n_override = 0
        if st.photo_path:                          # override только при фото (см. докстринг)
            for i in found:
                job = submit_card(i.brand, extract_model(i.name, i.brand),
                                  st.utp_text or "", st.photo_path)
                excel_store.update(item_key(i), status="card", card_job=job, tries=0)
                n_override += 1
        store.cancel(chat_id)
        mode = "карточка сразу, минуя research (своё фото)" if n_override \
            else "обычный конвейер (research → карточка)"
        return WizardReply(f"✅ поставлено в очередь: {len(found)} ({mode}). "
                           f"Статус: /excel")

    def handle_callback(chat_id: str, data: str) -> WizardReply | None:
        if not data.startswith("wizard:"):
            return None
        if data == "wizard:status":            # работает вне зависимости от диалога
            return WizardReply(excel_fn())
        st = store.snapshot(chat_id)
        if st is None:
            return WizardReply("❌ нет активного диалога — начните /task")
        action = data.split(":", 1)[1]
        if action == "skip_photo" and st.step == "awaiting_photo":
            store.set_photo(chat_id, None)
            return WizardReply("📝 Пришлите текст УТП или пропустите.", _SKIP_UTP_KB)
        if action == "skip_utp" and st.step == "awaiting_utp":
            store.set_utp(chat_id, None)
            return _confirm_prompt(store.snapshot(chat_id))
        if action == "cancel":
            store.cancel(chat_id)
            return WizardReply("❌ отменено")
        if action == "confirm" and st.step == "awaiting_confirm":
            return _do_confirm(chat_id, st)
        return WizardReply("❌ неожиданное действие для текущего шага")

    return start, handle_text, handle_photo, handle_callback
