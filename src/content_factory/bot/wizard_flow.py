"""Оркестрация визарда /task v2 (2026-07-07): категория (кнопки из прайса или
текст) → автосписок с номерами (или свой список строк — многострочный ввод) →
время выгрузки («🚀 сейчас» / «завтра 9:00») → (только для «сейчас») опц. фото →
опц. УТП → подтверждение.

Расписание: due_at пишется в excel_items — тик конвейера не берёт товар до
срока (ExcelStore.by_status). Фото/УТП-override доступен только в режиме
«сейчас»: submit_card дёргает агента немедленно и сломал бы расписание.

Чистая логика без Telegram: download/send инъецируются извне (bot/run.py)."""
from __future__ import annotations
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from content_factory.bot.commands import parse_due_at
from content_factory.ingest.excel_price import (
    load_price_slots, match_model_lines, search_items, top_sections,
    item_key, extract_model)
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
_CANCEL_KB = {"inline_keyboard": [[
    {"text": "❌ Отмена", "callback_data": "wizard:cancel"}]]}
_TIME_KB = {"inline_keyboard": [[
    {"text": "🚀 Сейчас", "callback_data": "wizard:time_now"}],
    [{"text": "❌ Отмена", "callback_data": "wizard:cancel"}]]}

_MAX_LIST = 30            # позиций в автосписке


@dataclass
class WizardReply:
    text: str
    markup: dict | None = None


_CATS_PER_PAGE = 24       # 12 рядов по 2: групп бывает 200+, все кнопки в одно
                          # сообщение Telegram не влезают — листаем страницами


def _category_keyboard(prices_dir, page: int = 0) -> dict | None:
    """Кнопки разделов прайсов, страница `page` (wizard:cat:<ГЛОБАЛЬНЫЙ индекс> —
    категории кириллицей не влезают в 64 байта callback_data, поэтому индекс в
    top_sections; листание — wizard:catpage:<n>). Все группы доступны."""
    sections = top_sections(prices_dir)
    if not sections:
        return None
    pages = max(1, -(-len(sections) // _CATS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    lo = page * _CATS_PER_PAGE
    btns = [{"text": s[:32], "callback_data": f"wizard:cat:{lo + k}"}
            for k, s in enumerate(sections[lo:lo + _CATS_PER_PAGE])]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    if pages > 1:                                  # ряд листания
        nav = []
        if page > 0:
            nav.append({"text": "◂ Назад", "callback_data": f"wizard:catpage:{page - 1}"})
        nav.append({"text": f"стр. {page + 1}/{pages}", "callback_data": "wizard:status"})
        if page < pages - 1:
            nav.append({"text": "Ещё ▸", "callback_data": f"wizard:catpage:{page + 1}"})
        rows.append(nav)
    rows.append([{"text": "➕ Свой товар", "callback_data": "wizard:manual"},
                 {"text": "📊 Статус", "callback_data": "wizard:status"}])
    return {"inline_keyboard": rows}


def make_wizard_flow(state_db, prices_dir, store, submit_card, save_photo, excel_fn,
                     now_fn=datetime.now):
    """submit_card(brand, model, utp, photo_path) -> job_id (см. card_submit.py).
    save_photo(chat_id, photo_bytes) -> абсолютный путь к сохранённому файлу.
    excel_fn() -> str — статус конвейера (кнопка «📊 Статус», не сбрасывает диалог).
    now_fn — инъекция часов для тестов расписания."""

    def _taken(excel_store: ExcelStore) -> set:
        return (PublishState(state_db).published_keys()
                | ConfirmStore(state_db).blocked_keys() | excel_store.all_keys())

    def _price_items():
        slots = load_price_slots(prices_dir)
        return [i for _, its in slots for i in its]

    def start(chat_id: str) -> WizardReply:
        store.start(chat_id)
        kb = _category_keyboard(prices_dir)
        if kb is None:
            return WizardReply(
                "🧾 Какая категория товара? Напишите текстом "
                "(напр.: стиральные машины).",
                {"inline_keyboard": [[
                    {"text": "➕ Свой товар", "callback_data": "wizard:manual"},
                    {"text": "📊 Статус", "callback_data": "wizard:status"}]]})
        return WizardReply("🧾 Выберите категорию кнопкой — пришлю список позиций "
                           "из прайсов. Или напишите категорию/список моделей текстом.",
                           kb)

    def _autolist(chat_id: str, category: str) -> WizardReply:
        excel_store = ExcelStore(state_db)
        found = search_items(_price_items(), category, _taken(excel_store),
                             limit=_MAX_LIST)
        if not found:
            return WizardReply(f"❌ по «{category}» в прайсах пусто (или всё уже "
                               f"в работе) — попробуйте другую категорию")
        cands = [(item_key(i), i.brand, extract_model(i.name, i.brand),
                  i.name, i.price) for i in found]
        store.set_candidates(chat_id, category, cands)
        listing = "\n".join(f"{n}. {c[3][:60]} — {c[4]:,} ₽".replace(",", " ")
                            for n, c in enumerate(cands, 1))
        return WizardReply(f"🔎 «{category}» — найдено {len(cands)}:\n{listing}\n\n"
                           f"Какие взять? Номера через пробел (напр.: 1 3 5) "
                           f"или «все».", _CANCEL_KB)

    def _time_prompt() -> WizardReply:
        return WizardReply("⏰ Когда выгружать? «🚀 Сейчас» — или напишите время: "
                           "«завтра 9:00», «сегодня 18:00», «08.07 10:30».", _TIME_KB)

    def _confirm_prompt(st) -> WizardReply:
        n = len(st.candidates or st.lines or [])
        when = "сейчас" if st.due_at is None else \
            datetime.fromtimestamp(st.due_at).strftime("%d.%m %H:%M")
        photo = "есть" if st.photo_path else "нет"
        utp = "есть" if st.utp_text else "нет"
        kb = {"inline_keyboard": list(_CONFIRM_KB["inline_keyboard"])}
        if st.due_at is None:              # «назад»: фото/УТП есть только в «сейчас»
            kb["inline_keyboard"] = [
                [{"text": "📎 Фото заново", "callback_data": "wizard:redo_photo"},
                 {"text": "📝 УТП заново", "callback_data": "wizard:redo_utp"}],
                *_CONFIRM_KB["inline_keyboard"]]
        if st.candidates:                  # наценка/скидка партии на лету
            kb["inline_keyboard"] = [
                [{"text": "💹 Наценка партии", "callback_data": "wizard:markup"}],
                *kb["inline_keyboard"]]
        return WizardReply(
            f"Категория: {st.category or '—'}\nПозиций: {n}\nВыгрузка: {when}\n"
            f"Фото: {photo} · УТП: {utp}\n\nПодтвердить постановку в очередь?",
            kb)

    def handle_text(chat_id: str, text: str) -> WizardReply | None:
        st = store.snapshot(chat_id)
        if st is None:
            return None                                    # не в мастере
        text = (text or "").strip()
        if text.startswith("/"):
            # команды (/auto /status /make …) проходят СКВОЗЬ визард к обработчику,
            # диалог не сбрасывается (грабля 2026-07-09: бот залип в awaiting_pick
            # и жрал все команды ответом «не понял номера»)
            return None

        if st.step == "awaiting_category":
            if not text:
                return WizardReply("❌ категория пустая — напишите текстом")
            lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
            if len(lines) > 1:                             # свой список моделей
                excel_store = ExcelStore(state_db)
                matches = match_model_lines(_price_items(), lines, _taken(excel_store))
                store.set_category(chat_id, "свой список")
                store.set_list(chat_id, lines)
                found = [m for m in matches if m.item]
                missing = [m for m in matches if not m.item]
                out = [f"✅ найдено {len(found)} из {len(matches)}:"]
                out += [f"— {m.item.name} · {m.item.price:,} ₽".replace(",", " ")
                        for m in found]
                if missing:
                    out.append(f"\n❌ не найдено ({len(missing)}):")
                    for m in missing:
                        cand = ", ".join(c.name[:40] for c in m.candidates) or "нет похожих"
                        out.append(f"— «{m.line[:50]}» (похоже: {cand})")
                reply = _time_prompt()
                return WizardReply("\n".join(out) + "\n\n" + reply.text, reply.markup)
            return _autolist(chat_id, text)                # категория → автосписок

        if st.step == "awaiting_manual_name":
            if not text:
                return WizardReply("❌ название пустое — напишите текстом", _CANCEL_KB)
            store.set_manual_name(chat_id, text)
            return WizardReply(f"✅ {text}\n💰 Теперь цена, ₽ — ответным сообщением, "
                               f"только число.",
                               {"force_reply": True,
                                "input_field_placeholder": "45990"})

        if st.step == "awaiting_manual_price":
            digits = re.sub(r"[^\d]", "", text)
            if not digits:
                return WizardReply("❌ не понял цену — только число, напр.: 45990",
                                   _CANCEL_KB)
            name = st.category or ""
            # свой товар = один «кандидат»: без бренда (карточке/research уходит
            # полное название), дальше стандартные шаги времени/фото/УТП
            key = "manual|" + re.sub(r"\s+", " ", name.lower()).strip()[:80]
            store.set_pick(chat_id, [(key, "", name, name, int(digits))])
            return _time_prompt()

        if st.step == "awaiting_markup":
            # ±проценты для всей партии: -10 скидка, +5 наценка (…90 сохраняем)
            from content_factory.pricing.pricing import round_up_90
            try:
                pct = float(text.replace(",", ".").replace("%", ""))
            except ValueError:
                return WizardReply("❌ только число со знаком: -10 скидка, "
                                   "+5 наценка", _CANCEL_KB)
            cands = [(k, b, m, n, round_up_90(p * (1 + pct / 100)))
                     for k, b, m, n, p in (tuple(c) for c in st.candidates or [])]
            store.update_prices(chat_id, cands)
            sign = f"{'+' if pct > 0 else ''}{pct:g}%"
            reply = _confirm_prompt(store.snapshot(chat_id))
            return WizardReply(f"💹 применено {sign} ко всей партии.\n\n" + reply.text,
                               reply.markup)

        if st.step == "awaiting_pick":
            if text.lower() in ("все", "всё", "all"):
                picked = list(st.candidates or [])
            else:
                nums = [int(t) for t in re.findall(r"\d+", text)]
                cands = st.candidates or []
                picked = [cands[i - 1] for i in nums if 1 <= i <= len(cands)]
            if not picked:
                return WizardReply("❌ не понял номера — напр.: 1 3 5, или «все»",
                                   _CANCEL_KB)
            store.set_pick(chat_id, picked)
            return _time_prompt()

        if st.step == "awaiting_time":
            due = parse_due_at(text, now_fn())
            if due is None:
                return WizardReply("❌ не понял время — напр.: «завтра 9:00», "
                                   "«18:30», или кнопка «🚀 Сейчас»", _TIME_KB)
            store.set_time(chat_id, due)
            return _confirm_prompt(store.snapshot(chat_id))

        if st.step == "awaiting_utp":
            store.set_utp(chat_id, text or None)
            return _confirm_prompt(store.snapshot(chat_id))

        return WizardReply("❌ сейчас жду не текст — см. предыдущее сообщение",
                           _CANCEL_KB)

    def handle_photo(chat_id: str, photo_bytes: bytes) -> WizardReply | None:
        st = store.snapshot(chat_id)
        if st is None or st.step != "awaiting_photo":
            return None
        path = save_photo(chat_id, photo_bytes)
        store.set_photo(chat_id, path)
        return WizardReply("📝 Пришлите текст УТП или пропустите.", _SKIP_UTP_KB)

    def _do_confirm(chat_id: str, st) -> WizardReply:
        excel_store = ExcelStore(state_db)
        if st.candidates:                                  # авто-путь: точные позиции
            rows = [tuple(c) for c in st.candidates]
        else:                                              # свой список строк
            matches = match_model_lines(_price_items(), st.lines or [],
                                        _taken(excel_store))
            rows = [(item_key(m.item), m.item.brand,
                     extract_model(m.item.name, m.item.brand),
                     m.item.name, m.item.price) for m in matches if m.item]
        if not rows:
            store.cancel(chat_id)
            return WizardReply("❌ ни одна позиция не подтвердилась "
                               "(возможно, уже в работе)")
        photo = None
        if st.photo_path and st.due_at is None:            # override только «сейчас»
            # resolve: отн. путь → от CWD бота (грабля 2026-07-09: card_submit
            # клеил его с output_dir агента → FileNotFoundError → crash-loop бота)
            photo = Path(st.photo_path).resolve()
            if not photo.exists():
                store.set_time(chat_id, None)              # назад на шаг фото
                return WizardReply("📎 Фото потерялось (файл не найден) — "
                                   "пришлите фото заново или пропустите.",
                                   _SKIP_PHOTO_KB)
        # Сабмит карточек ДО записи в конвейер: падение сабмита не должно
        # оставлять товар в status=new — иначе excel-тик утащит его в research
        # с чужим фото ChatGPT (грабля 2026-07-09, ларь Hyundai CH1002)
        jobs: list[tuple[str, int]] = []
        if photo is not None:
            for key, brand, model, name, price in rows:
                jobs.append((key, submit_card(brand, model, st.utp_text or "",
                                              str(photo))))
        excel_store.add_items(rows, due_at=st.due_at)
        n_override = 0
        for key, job in jobs:
            excel_store.update(key, status="card", card_job=job, tries=0)
            n_override += 1
        store.cancel(chat_id)
        if n_override:
            mode = "карточка сразу, минуя research (своё фото)"
        elif st.due_at is not None:
            mode = ("запланировано на "
                    + datetime.fromtimestamp(st.due_at).strftime("%d.%m %H:%M"))
        else:
            mode = "обычный конвейер (research → карточка)"
        return WizardReply(f"✅ поставлено в очередь: {len(rows)} ({mode}). "
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
        if action.startswith("catpage:") and st.step == "awaiting_category":
            try:
                page = int(action.split(":", 1)[1])
            except ValueError:
                page = 0
            kb = _category_keyboard(prices_dir, page=page)
            if kb is None:
                return WizardReply("❌ прайсы пусты — пришлите .xlsx файлом")
            return WizardReply("🧾 Выберите категорию кнопкой (или напишите "
                               "категорию/список моделей текстом).", kb)
        if action.startswith("cat:") and st.step == "awaiting_category":
            sections = top_sections(prices_dir)
            try:
                category = sections[int(action.split(":", 1)[1])]
            except (ValueError, IndexError):
                return WizardReply("❌ категория устарела — напишите текстом")
            return _autolist(chat_id, category)
        if action == "manual":
            # «Свой товар» стартует с ЛЮБОГО шага (грабля 2026-07-09: на шаге
            # списка кнопка падала в «неожиданное действие») — начинаем заново.
            # force_reply: Telegram открывает поле ввода с примером — владелец
            # принимал запрос названия с кнопкой «❌ Отмена» за ошибку
            store.start(chat_id)
            store.to_manual(chat_id)
            return WizardReply(
                "✍️ Напишите название товара ответным сообщением — одной строкой.\n"
                "Дальше спрошу: цена → время → фото (опц.) → УТП (опц.).\n"
                "Передумали — /task (начать заново).",
                {"force_reply": True,
                 "input_field_placeholder": "Кондиционер BORK AC-3001"})
        if action == "time_now" and st.step == "awaiting_time":
            store.set_time(chat_id, None)
            return WizardReply("📎 Пришлите фото (одно, на все позиции) "
                               "или пропустите.", _SKIP_PHOTO_KB)
        if action == "skip_photo" and st.step == "awaiting_photo":
            store.set_photo(chat_id, None)
            return WizardReply("📝 Пришлите текст УТП или пропустите.", _SKIP_UTP_KB)
        if action == "skip_utp" and st.step == "awaiting_utp":
            store.set_utp(chat_id, None)
            return _confirm_prompt(store.snapshot(chat_id))
        if action == "markup" and st.step == "awaiting_confirm":
            store.to_markup(chat_id)
            return WizardReply("💹 Наценка/скидка на всю партию — ответным "
                               "сообщением, число со знаком: -10 скидка, +5 наценка.",
                               {"force_reply": True, "input_field_placeholder": "-5"})
        if action == "redo_photo" and st.step == "awaiting_confirm":
            store.set_time(chat_id, None)              # назад на шаг фото («сейчас»)
            return WizardReply("📎 Пришлите новое фото (заменит прежнее) "
                               "или пропустите.", _SKIP_PHOTO_KB)
        if action == "redo_utp" and st.step == "awaiting_confirm":
            store.set_photo(chat_id, st.photo_path)    # тот же путь → шаг УТП
            return WizardReply("📝 Пришлите новый текст УТП (заменит прежний) "
                               "или пропустите.", _SKIP_UTP_KB)
        if action == "cancel":
            store.cancel(chat_id)
            return WizardReply("❌ отменено")
        if action == "confirm" and st.step == "awaiting_confirm":
            return _do_confirm(chat_id, st)
        return WizardReply("❌ неожиданное действие для текущего шага")

    return start, handle_text, handle_photo, handle_callback
