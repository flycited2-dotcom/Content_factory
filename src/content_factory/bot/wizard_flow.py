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
import hashlib
import inspect
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from content_factory.bot.commands import parse_due_at
from content_factory.bot.catalog_tree import build_tree, find_node
from content_factory.ingest.source_names import source_name
from content_factory.ingest.excel_price import (
    load_price_slots, match_model_lines, search_items, top_sections,
    item_key, item_is_taken, legacy_item_key, PriceItem, extract_model)
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

_MAX_LIST = 20            # позиций на странице; полный список хранится в SQLite

# Упреждение расписания: владелец задаёт время ГОТОВНОСТИ фото, а тик берёт
# товар только когда due_at дозрел — без упреждения генерация СТАРТУЕТ в
# заданный час (2026-07-10: «к 9:00» начало генериться в 9:00, фото к 10:00).
# В excel_items пишем срок с упреждением: 6 мин/позиция, минимум час.
TASK_LEAD_SECONDS = 3600
_LEAD_PER_ITEM_SECONDS = 360


@dataclass
class WizardReply:
    text: str
    markup: dict | None = None


_CATS_PER_PAGE = 10


def _category_key(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]


def _selection_token(candidates) -> str:
    return _category_key("\n".join(str(candidate[0]) for candidate in candidates))


def _unique_count(items):
    return len({item_key(item) for item in items})


def _pick_keyboard(count: int) -> dict:
    sizes = sorted({min(n, count) for n in (1, 5, 10) if count})
    rows = [[{"text": f"✅ Взять {n}", "callback_data": f"wizard:pick_first:{n}"} for n in sizes]]
    if count:
        rows.append([{"text": f"✅ Все {count}", "callback_data": f"wizard:pick_first:{count}"}])
    rows.append([{"text": "✏️ Своё число", "callback_data": "wizard:pick_count"}])
    rows.append([{"text": "◀️ Другие категории", "callback_data": "wizard:categories"},
                 {"text": "❌ Отмена", "callback_data": "wizard:cancel"}])
    return {"inline_keyboard": rows}


def _catalog(prices_dir, source_slot=None):
    items = [item for slot, rows in load_price_slots(prices_dir, for_telegram=True)
             if source_slot is None or slot == source_slot for item in rows]
    if source_slot is None:  # migration: already active legacy drafts
        sections = top_sections(prices_dir)
    else:
        from collections import Counter
        sections = [name for name, _ in Counter(item.section for item in items
                    if item.section.strip()).most_common()]
    return items, build_tree(items, sections)


def _source_keyboard(prices_dir, page=0):
    slots = [(slot, rows) for slot, rows in load_price_slots(prices_dir, for_telegram=True) if rows]
    pages = max(1, -(-len(slots) // _CATS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    lo = page * _CATS_PER_PAGE
    rows = [[{"text": f"{source_name(prices_dir, slot)} · {_unique_count(items)}",
              "callback_data": f"wizard:source:{_category_key(slot)}"}]
            for slot, items in slots[lo:lo + _CATS_PER_PAGE]]
    if pages > 1:
        navigation = []
        if page:
            navigation.append({"text": "◂ Назад", "callback_data": f"wizard:sourcepage:{page - 1}"})
        if page < pages - 1:
            navigation.append({"text": "Ещё ▸", "callback_data": f"wizard:sourcepage:{page + 1}"})
        rows.append(navigation)
    rows.append([{"text": "➕ Свой товар", "callback_data": "wizard:manual"},
                 {"text": "📊 Статус", "callback_data": "wizard:status"}])
    rows.append([{"text": "❌ Отмена", "callback_data": "wizard:cancel"}])
    return {"inline_keyboard": rows}


def _numbered_selection(text, count):
    """Strict number/range input; keep input order and never silently drop errors."""
    text = text.strip()
    if not re.fullmatch(r"\d+(?:\s*[-–]\s*\d+)?(?:[\s,;]+\d+(?:\s*[-–]\s*\d+)?)*", text):
        return None
    picked = []
    for match in re.finditer(r"(\d+)(?:\s*[-–]\s*(\d+))?", text):
        lo = int(match[1])
        hi = int(match[2]) if match[2] else lo
        if not 1 <= lo <= hi <= count:
            return None
        picked.extend(range(lo, hi + 1))
    return list(dict.fromkeys(picked))


def _category_keyboard(prices_dir, page: int = 0, node_id: str = "", source_slot=None) -> dict | None:
    """Root → subgroups → products, with stable supplier ids and full pagination."""
    items, nodes = _catalog(prices_dir, source_slot)
    source_prefix = f"{_category_key(source_slot)}:" if source_slot else ""
    node = nodes.get(node_id, nodes[""])
    children = node.children
    if not children:
        return None
    pages = max(1, -(-len(children) // _CATS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    lo = page * _CATS_PER_PAGE
    btns = [{"text": nodes[key].name,
             "callback_data": f"wizard:cat:{source_prefix}{_category_key(key)}"}
            for key in children[lo:lo + _CATS_PER_PAGE]]
    rows = [[b] for b in btns]
    if pages > 1:                                  # ряд листания
        nav = []
        prefix = (f"wizard:treepage:{source_prefix}{_category_key(node.identity)}:"
                  if node.identity else f"wizard:catpage:{source_prefix}")
        if page > 0:
            nav.append({"text": "◂ Назад", "callback_data": f"{prefix}{page - 1}"})
        nav.append({"text": f"стр. {page + 1}/{pages}", "callback_data": "wizard:status"})
        if page < pages - 1:
            nav.append({"text": "Ещё ▸", "callback_data": f"{prefix}{page + 1}"})
        rows.append(nav)
    if node.identity:
        rows.append([{"text": f"📋 Товары всей группы · {_unique_count(items[index] for index in node.item_indices)}",
                      "callback_data": f"wizard:catitems:{source_prefix}{_category_key(node.identity)}"}])
        rows.append([{"text": "◀️ На уровень выше",
                      "callback_data": (f"wizard:cat:{source_prefix}{_category_key(node.parent)}"
                                        if node.parent else "wizard:categories")}])
    rows.append([{"text": "◀️ Поставщики", "callback_data": "wizard:sources"}])
    rows.append([{"text": "➕ Свой товар", "callback_data": "wizard:manual"},
                 {"text": "📊 Статус", "callback_data": "wizard:status"}])
    return {"inline_keyboard": rows}


def make_wizard_flow(state_db, prices_dir, store, submit_card, save_photo, excel_fn,
                     now_fn=datetime.now, queue_db=None, wake_fn=None):
    """submit_card(brand, model, utp, photo_path) -> job_id (см. card_submit.py).
    save_photo(chat_id, photo_bytes) -> абсолютный путь к сохранённому файлу.
    excel_fn() -> str — статус конвейера (кнопка «📊 Статус», не сбрасывает диалог).
    now_fn — инъекция часов для тестов расписания."""

    def _taken(excel_store: ExcelStore) -> set:
        return (PublishState(state_db).published_keys()
                | ConfirmStore(state_db).blocked_keys()
                | excel_store.selection_blocked_keys(queue_db))

    def _price_items(chat_id):
        st = store.snapshot(chat_id)
        slots = load_price_slots(prices_dir, for_telegram=True)
        return [item for slot, items in slots
                if st.source_slot is None or slot == st.source_slot for item in items]

    def _source_prompt(chat_id, page=0):
        store.to_source(chat_id)
        return WizardReply("🎬 Задача Контент-заводу. Для Avito: /avito categories.\n"
                           "📦 Выберите поставщика — рядом число доступных товаров. "
                           "Затем выберем категорию и подгруппу.",
                           _source_keyboard(prices_dir, page))

    def _category_prompt(chat_id, page=0, node_id=""):
        st = store.snapshot(chat_id)
        if st.source_slot is None:
            return _source_prompt(chat_id)
        slot = st.source_slot
        store.start(chat_id, source_slot=slot)
        kb = _category_keyboard(prices_dir, page=page, node_id=node_id, source_slot=slot)
        if kb is None:
            return WizardReply("У выбранного поставщика нет доступных категорий. "
                               "Выберите другого поставщика.", _source_keyboard(prices_dir))
        return WizardReply(f"📦 {source_name(prices_dir, slot)}\n"
                           "🧾 Выберите категорию кнопкой или напишите "
                           "категорию/список моделей текстом.", kb)

    def start(chat_id: str) -> WizardReply:
        return _source_prompt(chat_id)

    def _count_prompt(st):
        return WizardReply(f"✏️ Сколько первых товаров взять? Введите целое число "
                           f"от 1 до {len(st.candidates or [])}.",
                           {"inline_keyboard": [[{"text": "◀️ К списку товаров",
                             "callback_data": "wizard:count_back"},
                             {"text": "❌ Отмена", "callback_data": "wizard:cancel"}]]})

    def _candidate_reply(chat_id: str) -> WizardReply:
        st = store.snapshot(chat_id)
        cands = st.candidates or []
        pages = max(1, -(-len(cands) // _MAX_LIST))
        page = min(st.page, pages - 1)
        lo, hi = page * _MAX_LIST, min((page + 1) * _MAX_LIST, len(cands))
        selected = set(st.selected_keys or [])
        token = _selection_token(cands)
        listing = "\n".join(
            f"{'✅' if c[0] in selected else '▫️'} {n}. {c[3][:60]} — {c[4]:,} ₽".replace(",", " ")
            for n, c in enumerate(cands[lo:hi], lo + 1))
        rows = [[{"text": f"{'✅' if cands[n - 1][0] in selected else '▫️'} {n}",
                  "callback_data": f"wizard:toggle:{token}:{n}"}
                 for n in range(start, min(start + 5, hi + 1))]
                for start in range(lo + 1, hi + 1, 5)]
        if pages > 1:
            nav = []
            if page:
                nav.append({"text": "◂ Назад", "callback_data": f"wizard:itempage:{token}:{page - 1}"})
            if page < pages - 1:
                nav.append({"text": "Ещё ▸", "callback_data": f"wizard:itempage:{token}:{page + 1}"})
            rows.append(nav)
        rows.append([{"text": "✅ Выбрать страницу", "callback_data": f"wizard:page_select:{token}:{page}"},
                     {"text": "☐ Снять страницу", "callback_data": f"wizard:page_clear:{token}:{page}"}])
        if selected:
            rows.append([{"text": f"Продолжить с выбранными · {len(selected)}",
                          "callback_data": f"wizard:selection_continue:{token}"},
                         {"text": "☐ Снять всё", "callback_data": f"wizard:selection_clear:{token}"}])
        rows.extend(_pick_keyboard(len(cands))["inline_keyboard"])
        return WizardReply(
            f"🔎 «{st.category}» — доступно {len(cands)}. Стр. {page + 1}/{pages}. "
            f"Выбрано: {len(selected)}.\n{listing}\n\n"
            "Отметьте номера кнопками; выбор сохраняется при листании. "
            "Затем «Продолжить с выбранными». Можно отправить номера "
            "«3 14 32» или диапазон «10-15» текстом.",
            {"inline_keyboard": rows})

    def _autolist(chat_id: str, category: str, exact_items=None) -> WizardReply:
        excel_store = ExcelStore(state_db)
        blocked = _taken(excel_store)
        if exact_items is None:
            found = search_items(_price_items(chat_id), category, blocked, limit=10 ** 9)
        else:
            found = list({item_key(item): item for item in exact_items
                          if not item_is_taken(item, blocked)}.values())
        if not found:
            return WizardReply(f"По «{category}» нет свободных позиций для новой задачи. "
                               "Товары могут быть уже в работе или иметь готовые карточки. "
                               "Проверить: /excel. Отменённые задачи можно выбрать снова "
                               "после завершения уже запущенного фотоагента.")
        cands = [(item_key(i), i.brand, extract_model(i.name, i.brand),
                  i.name, i.price) for i in found]
        store.set_candidates(chat_id, category, cands)
        return _candidate_reply(chat_id)

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

        if st.step == "awaiting_source":
            return WizardReply("Сначала выберите поставщика кнопкой.", _source_keyboard(prices_dir))

        if st.step == "awaiting_count":
            if not re.fullmatch(r"[0-9]+", text) or not 1 <= int(text) <= len(st.candidates or []):
                return _count_prompt(st)
            store.set_pick(chat_id, list(st.candidates[:int(text)]))
            return _time_prompt()

        if st.step == "awaiting_category":
            if not text:
                return WizardReply("❌ категория пустая — напишите текстом")
            lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
            if len(lines) > 1:                             # свой список моделей
                excel_store = ExcelStore(state_db)
                matches = match_model_lines(_price_items(chat_id), lines, _taken(excel_store))
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
                cands = st.candidates or []
                nums = _numbered_selection(text, len(cands))
                picked = [cands[i - 1] for i in nums] if nums else []
            if not picked:
                return WizardReply("❌ укажите номера из списка — напр.: 1 3 5, диапазон 10-15, или «все»",
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
            matches = match_model_lines(_price_items(chat_id), st.lines or [],
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
        requested = len({row[0] for row in rows})
        blocked = _taken(excel_store)
        rows = [row for row in rows if row[0] not in blocked
                and not (row[0].startswith("excel|aru:") and legacy_item_key(
                    PriceItem("", "", row[1], row[3], row[4])) in blocked)]
        start_at = None
        if st.due_at is not None:
            lead = max(TASK_LEAD_SECONDS, _LEAD_PER_ITEM_SECONDS * len(rows))
            start_at = st.due_at - lead
        # Claim before any paid request. A failed photo submission remains outside
        # the research pipeline; repeated confirmations cannot send it twice.
        rows = excel_store.select_items(rows, due_at=start_at, queue_db=queue_db,
                                        reservation=photo is not None)
        if not rows:
            store.cancel(chat_id)
            from content_factory.bot.task_status import audit_task_event
            audit_task_event(state_db, "selection", detail={"requested": requested, "accepted": 0})
            return WizardReply("Новых задач добавлено: 0. Выбранные позиции уже "
                               "заняты или ждут завершения фотоагента. Статус: /excel")
        n_override = 0
        if photo is not None:
            try:
                for key, brand, model, name, price in rows:
                    if excel_store.get(key).status != "submission":
                        continue  # Finished research/card is resumed without payment.
                    request_key = excel_store.get_or_create_request_key(key, "card")
                    parameters = inspect.signature(submit_card).parameters.values()
                    with_identity = any(p.name == "request_key" or
                                        p.kind == inspect.Parameter.VAR_KEYWORD
                                        for p in parameters)
                    kwargs = {"request_key": request_key} if with_identity else {}
                    job = submit_card(brand, model, st.utp_text or "", str(photo), **kwargs)
                    active = excel_store.bind_submitted_job(
                        key, "card", job, "submission", tries=0, request_key=request_key)
                    if active:
                        n_override += 1
                    elif excel_store.get(key).status == "cancelled":
                        from content_factory.bot.task_status import cancel_known_job
                        cancel_known_job(queue_db, job, request_key)
            except Exception as exc:
                for row in rows:
                    if excel_store.get(row[0]).status == "submission":
                        excel_store.update(row[0], status="submission_failed",
                                           error=f"manual_card_submission:{type(exc).__name__}")
                raise
        # УТП владельца — в research_cache (source='manual', research его не
        # перезапишет): превью строит «Ключевые особенности» из кэша, и для
        # ручного товара со своим УТП подпись выходила ПУСТОЙ (2026-07-10,
        # сплит Daicond — на карточке УТП есть, в подписи нет)
        if st.utp_text:
            for key, brand, model, name, price in rows:
                excel_store.cache_put(
                    f"{brand.strip().lower()}|{model.strip().lower()}",
                    st.utp_text, None, source="manual")
        store.cancel(chat_id)
        from content_factory.bot.task_status import audit_task_event
        audit_task_event(state_db, "selection", [row[0] for row in rows],
                         {"requested": requested, "accepted": len(rows), "photo_override": n_override})
        if n_override:
            mode = "карточка сразу, минуя research (своё фото)"
        elif st.due_at is not None:
            mode = ("фото к "
                    + datetime.fromtimestamp(st.due_at).strftime("%d.%m %H:%M")
                    + ", генерация с "
                    + datetime.fromtimestamp(start_at).strftime("%d.%m %H:%M"))
        else:
            mode = "обычный конвейер (research → карточка)"
        skipped = f" Уже занято: {requested - len(rows)}." if requested > len(rows) else ""
        # A whole supplier group can contain thousands of rows. Keep the Telegram
        # receipt bounded; every selected row is still stored in the queue.
        listing = "\n".join(f"• {row[3][:100]}" for row in rows[:20])
        if len(rows) > 20:
            listing += f"\n… ещё {len(rows) - 20} поз. Полная партия: /excel."
        wake_note = ""
        if wake_fn is not None and (start_at is None or start_at <= now_fn().timestamp()):
            try:
                requested_run = bool(wake_fn())
            except Exception:
                requested_run = False
            wake_note = ("\nЗапуск конвейера запрошен; этапы появятся в /excel."
                         if requested_run else
                         "\nЗадачи сохранены. Немедленный запуск не подтверждён; "
                         "автопроверка повторяется каждую минуту. Статус: /excel")
        return WizardReply(f"✅ поставлено в очередь: {len(rows)} ({mode}).{skipped}\n"
                           f"{listing}\nСтатус этой партии и всей очереди: /excel{wake_note}")

    def handle_callback(chat_id: str, data: str) -> WizardReply | None:
        if not data.startswith("wizard:"):
            return None
        if data == "wizard:status":            # работает вне зависимости от диалога
            return WizardReply(excel_fn())
        action = data.split(":", 1)[1]
        st = store.snapshot(chat_id)
        if action == "sources" or action.startswith("sourcepage:"):
            try:
                page = int(action.split(":", 1)[1]) if ":" in action else 0
            except ValueError:
                page = 0
            return _source_prompt(chat_id, page)
        if action.startswith("source:"):
            key = action.split(":", 1)[1]
            slot = next((slot for slot, items in load_price_slots(prices_dir, for_telegram=True)
                         if items and _category_key(slot) == key), None)
            if slot is None:
                result = _source_prompt(chat_id)
                result.text = "Поставщик в старом меню устарел. Выберите его заново."
                return result
            store.set_source(chat_id, slot)
            return _category_prompt(chat_id)
        if st is None:
            if action == "categories" or action.startswith(("cat:", "catpage:", "treepage:", "catitems:")):
                return _source_prompt(chat_id)
            return WizardReply("❌ нет активного диалога — начните /task")
        category_action = action.startswith(("cat:", "catitems:", "catpage:", "treepage:"))
        if category_action and st.source_slot:
            parts = action.split(":")
            if len(parts) < 3 or parts[1] != _category_key(st.source_slot):
                return WizardReply("Меню другого поставщика устарело. Продолжите выбор текущего поставщика.",
                                   _category_keyboard(prices_dir, source_slot=st.source_slot))
            action = parts[0] + ":" + ":".join(parts[2:])
        if (category_action or action == "categories") and st.step == "awaiting_source":
            return WizardReply("Сначала выберите поставщика.", _source_keyboard(prices_dir))
        if action == "categories" or action.startswith(("catpage:", "treepage:")):
            node_id = ""
            try:
                if action.startswith("treepage:"):
                    _, key, raw_page = action.split(":", 2)
                    _, nodes = _catalog(prices_dir, st.source_slot)
                    node = find_node(nodes, key)
                    if node is None:
                        return WizardReply("Категория в старом меню устарела. Выберите её заново.",
                                           _category_keyboard(prices_dir, source_slot=st.source_slot))
                    node_id = node.identity
                    page = int(raw_page)
                else:
                    page = int(action.split(":", 1)[1]) if ":" in action else 0
            except ValueError:
                page = 0
            return _category_prompt(chat_id, page, node_id)
        if action.startswith(("cat:", "catitems:")):
            items, nodes = _catalog(prices_dir, st.source_slot)
            key = action.split(":", 1)[1]
            node = find_node(nodes, key)
            if node is None:
                store.start(chat_id, source_slot=st.source_slot)
                return WizardReply("Категория в старом меню устарела. Выберите её заново.",
                                   _category_keyboard(prices_dir, source_slot=st.source_slot))
            store.start(chat_id, source_slot=st.source_slot)
            category = " / ".join(node.path)
            if node.children and not action.startswith("catitems:"):
                return WizardReply(f"📂 {category}\nВыберите подгруппу. "
                                   f"Товаров в наличии: {_unique_count(items[index] for index in node.item_indices)}.",
                                   _category_keyboard(prices_dir, node_id=node.identity, source_slot=st.source_slot))
            return _autolist(chat_id, category,
                             [items[index] for index in node.item_indices])
        if action == "pick_count" and st.step == "awaiting_pick":
            store.to_count(chat_id)
            return _count_prompt(store.snapshot(chat_id))
        if action == "count_back" and st.step == "awaiting_count":
            store.to_pick(chat_id)
            return _candidate_reply(chat_id)
        selection_action = action.split(":", 1)[0]
        if st.step == "awaiting_pick" and selection_action in (
            "itempage", "toggle", "page_select", "page_clear", "selection_clear", "selection_continue"
        ):
            parts = action.split(":")
            if len(parts) < 2 or parts[1] != _selection_token(st.candidates or []):
                return WizardReply("Этот список товаров устарел. Продолжите выбор в текущем списке.",
                                   _candidate_reply(chat_id).markup)
        if st.step == "awaiting_pick" and action.startswith("itempage:"):
            try:
                page = int(action.split(":")[2])
            except (ValueError, IndexError):
                page = st.page
            pages = max(1, -(-len(st.candidates or []) // _MAX_LIST))
            store.set_browse(chat_id, page=max(0, min(page, pages - 1)))
            return _candidate_reply(chat_id)
        if st.step == "awaiting_pick" and (
            action.startswith("toggle:") or selection_action in
            ("page_select", "page_clear", "selection_clear", "selection_continue")
        ):
            cands = st.candidates or []
            selected = set(st.selected_keys or [])
            known = {candidate[0] for candidate in cands}
            selected &= known
            if selection_action == "selection_continue":
                picked = [candidate for candidate in cands if candidate[0] in selected]
                if not picked:
                    return WizardReply("Сначала отметьте товары.", _candidate_reply(chat_id).markup)
                store.set_pick(chat_id, picked)
                return _time_prompt()
            if selection_action == "selection_clear":
                selected.clear()
            elif selection_action in ("page_select", "page_clear"):
                try:
                    target_page = int(action.split(":")[2])
                except (ValueError, IndexError):
                    target_page = st.page
                page_keys = {candidate[0] for candidate in
                             cands[target_page * _MAX_LIST:(target_page + 1) * _MAX_LIST]}
                if selection_action == "page_select":
                    selected |= page_keys
                else:
                    selected -= page_keys
            else:
                try:
                    index = int(action.split(":")[2]) - 1
                except (ValueError, IndexError):
                    index = -1
                if 0 <= index < len(cands):
                    key = cands[index][0]
                    selected.symmetric_difference_update({key})
            store.set_browse(chat_id, selected_keys=[candidate[0] for candidate in cands
                                                     if candidate[0] in selected])
            return _candidate_reply(chat_id)
        if action.startswith("pick_first:") and st.step == "awaiting_pick":
            try:
                count = int(action.split(":", 1)[1])
            except ValueError:
                count = 0
            if not 1 <= count <= len(st.candidates or []):
                return WizardReply("Выберите количество из текущего списка.",
                                   _pick_keyboard(len(st.candidates or [])))
            store.set_pick(chat_id, list(st.candidates[:count]))
            return _time_prompt()
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
        if st.step == "awaiting_pick":
            return WizardReply("Список уже открыт. Выберите количество кнопкой или отправьте номера.",
                               _candidate_reply(chat_id).markup)
        if st.step == "awaiting_count":
            return _count_prompt(st)
        if st.step == "awaiting_time":
            return _time_prompt()
        if st.step == "awaiting_photo":
            return WizardReply("Сейчас нужен снимок товара или «Пропустить».", _SKIP_PHOTO_KB)
        if st.step == "awaiting_utp":
            return WizardReply("Сейчас нужен текст УТП или «Пропустить».", _SKIP_UTP_KB)
        if st.step == "awaiting_confirm":
            return _confirm_prompt(st)
        return WizardReply("Эта кнопка относится к предыдущему шагу. Выберите категорию заново.",
                           {"inline_keyboard": [[{"text": "📦 Категории", "callback_data": "wizard:categories"}]]})

    return start, handle_text, handle_photo, handle_callback
