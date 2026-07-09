"""Telegram-бот (long-poll). Принимает команды ТОЛЬКО от владельца (chat_id из .env),
маршрутизирует через bot.commands.handle_command. Команда /approve публикует подтверждённый
пост в канал (confirm-пилот). Запуск как сервис `cf-bot`.

  python -m content_factory.bot.run
"""
from __future__ import annotations
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
import httpx
from decouple import config

from content_factory.config import load_config
from content_factory.orchestrator.auto import auto_command, auto_enabled
from content_factory.publish.telegram import publish_post, PublishState, TG_API
from content_factory.publish.orders import OrderLinks, order_markup
from content_factory.bot.order_dialog import OrderDialogStore
from content_factory.bot.order_flow import make_order_flow
from content_factory.orchestrator.queue import TaskQueue
from content_factory.orchestrator.confirm_store import ConfirmStore
from content_factory.bot.commands import handle_command, handle_callback
from content_factory.bot.voice import transcribe_voice_bytes
from content_factory.bot.cmd_input import (
    bare_arg_command, prompt_for, resolve_reply, PendingCmdStore)


def make_publish_fn(token: str, parse_mode: str, pub_state: PublishState, http=None,
                    order_bot: str = "", links: OrderLinks | None = None):
    """publish_fn(awaiting) → публикует подтверждённый пост в его канал
    (+ url-кнопка «📩 Заказать», если настроен order_bot)."""
    def publish_fn(a):
        markup = None
        if order_bot and links is not None:
            markup = order_markup(order_bot, links.code_for(a.key))
        return publish_post(token, a.channel, a.card_path, a.caption, http=http,
                            parse_mode=parse_mode, key=a.key, state=pub_state, retries=2,
                            reply_markup=markup)
    return publish_fn


def make_regen_fn(card_jobs_db, state_db):
    """regen_fn(awaiting) → убрать карточку для перегенерации: удалить файл и запись
    CardJobStore (ключ store = имя файла карточки без расширения). После этого серия
    снова «без карточки» — cf-cards пересабмитит её агенту на ближайшем тике.
    Excel-товар (ключ excel|*) дополнительно возвращается в status='new' — иначе
    он оставался в preview, а excel-тик пересобирает только new/research/card,
    и перегенерация не происходила НИКОГДА (грабля 2026-07-06); research возьмётся
    из кэша (research_cache), так что сразу пойдёт новая карточка."""
    def regen_fn(a) -> bool:
        p = Path(a.card_path or "")
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            with sqlite3.connect(card_jobs_db) as c:
                c.execute("DELETE FROM card_jobs WHERE key=?", (p.stem,))
        except sqlite3.OperationalError:
            pass                                   # store ещё не создан — нечего чистить
        if (a.key or "").startswith("excel|"):
            try:
                with sqlite3.connect(state_db) as c:
                    c.execute("UPDATE excel_items SET status='new', research_job=NULL, "
                              "card_job=NULL, tries=0 WHERE key=?", (a.key,))
            except sqlite3.OperationalError:
                pass                               # таблицы ещё нет — не excel-путь
        return True
    return regen_fn


def make_make_fn(state_db, prices_dir):
    """make_fn(count, category, quotas) для /make: выбрать позиции из последнего
    прайса и поставить в excel-конвейер (research → карточка → превью)."""
    def make_fn(count, category, quotas):
        from content_factory.ingest.excel_price import (
            load_price_slots, select_from_price, item_key, extract_model,
            load_search_aliases)
        from content_factory.orchestrator.excel_pipeline import ExcelStore
        from content_factory.orchestrator.confirm_store import ConfirmStore
        slots = load_price_slots(prices_dir)
        if not slots:
            return "❌ прайс не загружен — пришлите .xlsx файлом в этот чат"
        items = [i for _, its in slots for i in its]      # свой прайс приоритетнее почтового
        store = ExcelStore(state_db)
        taken = (PublishState(state_db).published_keys()
                 | ConfirmStore(state_db).blocked_keys() | store.all_keys())
        got = select_from_price(items, category, quotas, count, taken,
                                aliases=load_search_aliases(Path("config/search_aliases.yaml")))
        if not got:
            return f"❌ по запросу «{category}» ничего не нашлось (или всё уже в работе)"
        rows = [(item_key(i), i.brand, extract_model(i.name, i.brand), i.name, i.price)
                for i in got]
        n = store.add_items(rows)
        listing = "\n".join(
            f"— {(i.brand + ' ') if i.brand else ''}{extract_model(i.name, i.brand)}"
            f" · {i.price:,} ₽".replace(",", " ") for i in got)
        short = f" (запрошено {count}, нашлось только {len(got)})" if len(got) < count else ""
        return (f"✅ выбрано {len(got)}{short} (новых в работу: {n}):\n{listing}\n\n"
                f"Конвейер: УТП+фото → карточка → превью сюда (тик ~10 мин). Статус: /excel")
    return make_fn


def make_find_pick_fns(state_db, prices_dir):
    """/find <фраза> — нумерованный список кандидатов из прайса;
    /pick 1 3 5 — поставить выбранные в конвейер; /excel — статус конвейера."""
    from content_factory.ingest.excel_price import (
        load_price_slots, search_items, item_key, extract_model, load_search_aliases)
    from content_factory.orchestrator.excel_pipeline import ExcelStore
    from content_factory.orchestrator.confirm_store import ConfirmStore

    aliases_path = Path("config/search_aliases.yaml")

    def _taken(store):
        return (PublishState(state_db).published_keys()
                | ConfirmStore(state_db).blocked_keys() | store.all_keys())

    def _pick_table(c):
        c.execute("CREATE TABLE IF NOT EXISTS pick_list (idx INTEGER PRIMARY KEY, "
                  "key TEXT, brand TEXT, model TEXT, name TEXT, price INTEGER)")

    def find_fn(phrase):
        slots = load_price_slots(prices_dir)
        if not slots:
            return "❌ прайс не загружен — пришлите .xlsx файлом"
        items = [i for _, its in slots for i in its]      # свой прайс приоритетнее почтового
        store = ExcelStore(state_db)
        found = search_items(items, phrase, _taken(store), limit=30,
                             aliases=load_search_aliases(aliases_path))
        if not found:
            return (f"❌ по «{phrase}» ничего не нашлось (или всё уже в работе).\n"
                    f"Подсказка: слова ищутся без окончаний, можно сужать: "
                    f"/find генераторы инверторные carver")
        with sqlite3.connect(state_db) as c:
            _pick_table(c)
            c.execute("DELETE FROM pick_list")
            for n, i in enumerate(found, 1):
                c.execute("INSERT INTO pick_list VALUES(?,?,?,?,?,?)",
                          (n, item_key(i), i.brand, extract_model(i.name, i.brand),
                           i.name, i.price))
        listing = "\n".join(f"{n}. {i.name} — {i.price:,} ₽".replace(",", " ")
                            for n, i in enumerate(found, 1))
        return (f"🔎 Найдено {len(found)} по «{phrase}»:\n{listing}\n\n"
                f"Взять в работу: /pick 1 3 5 (номера)")

    def pick_fn(nums):
        with sqlite3.connect(state_db) as c:
            _pick_table(c)
            rows = c.execute(
                f"SELECT key, brand, model, name, price FROM pick_list "
                f"WHERE idx IN ({','.join('?' * len(nums))})", nums).fetchall()
        if not rows:
            return "❌ таких номеров нет — сначала /find <фраза>"
        store = ExcelStore(state_db)
        n = store.add_items(rows)
        listing = "\n".join(f"— {r[3]} · {r[4]:,} ₽".replace(",", " ") for r in rows)
        return (f"✅ взято в работу {n} из {len(rows)}:\n{listing}\n\n"
                f"Конвейер: УТП+фото → карточка → превью сюда (тик ~10 мин). Статус: /excel")

    def excel_fn(arg: str | None = None):
        store = ExcelStore(state_db)
        # /excel retry — вернуть failed в конвейер с чистого листа (2026-07-07)
        if arg in ("retry", "повтор", "повторить"):
            n = store.retry_failed()
            if not n:
                return "✅ failed-позиций нет — повторять нечего"
            return (f"🔁 возвращено в конвейер: {n} (research заново, "
                    f"тик ~10 мин). Статус: /excel")
        counts = {s: len(store.by_status(s))
                  for s in ("new", "research", "card", "preview", "failed")}
        lines = [f"Конвейер прайса: 🆕 {counts['new']} · 🔎 research {counts['research']} · "
                 f"🎨 card {counts['card']} · 👀 превью {counts['preview']} · "
                 f"❌ failed {counts['failed']}"]
        # секции с разделителями, ошибки — первой строкой (многострочные Call log
        # Playwright сливали статус в нечитаемую простыню — жалоба 2026-07-07)
        for s, mark, title in (("research", "🔎", "На research"),
                               ("card", "🎨", "Рисуются карточки"),
                               ("failed", "❌", "Ошибки")):
            items = store.by_status(s)[:5]
            if not items:
                continue
            lines.append("━" * 22)
            lines.append(f"{mark} {title}:")
            for i in items:
                extra = ""
                if s == "failed" and i.error:
                    extra = f" — {i.error.splitlines()[0][:70]}"
                lines.append(f"• {i.brand} {i.model}{extra}".strip())
        if counts["failed"]:
            lines.append("↻ Вернуть ошибки в работу: /excel retry")
        return "\n".join(lines)

    return find_fn, pick_fn, excel_fn


def make_price_fn(state_db, token: str, review_channel: str, parse_mode: str,
                  links, http=None):
    """price_fn(key, new_price) — ручная цена для превью на подтверждении
    (кнопка «💰 Изменить цену», запрос владельца 2026-07-07: акции на единичный
    товар). Подпись обновляется, статус сбрасывается в pending, свежее превью
    пересылается в ревью-канал со штатными кнопками. Только excel|* — у серий
    кондиционеров цены линейкой, их не трогаем."""
    from content_factory.orchestrator.confirm_store import ConfirmStore
    from content_factory.orchestrator.excel_run import (
        replace_price_in_caption, preview_markup, _money)

    def price_fn(key: str, new_price: int) -> str:
        if not key.startswith("excel|"):
            return "❌ смена цены доступна только товарам из прайса (не сериям)"
        cs = ConfirmStore(state_db)
        a = cs.get(key)
        if a is None:
            return f"❌ нет превью на подтверждении: {key}"
        caption = replace_price_in_caption(a.caption, new_price)
        cs.add(key, a.channel, a.card_path, caption)     # upsert → снова pending
        kb = json.dumps(preview_markup(links.code_for(key)), ensure_ascii=False)
        res = publish_post(token, review_channel, a.card_path,
                           f"{caption}\n\n— на подтверждение (цена изменена) —",
                           http=http, parse_mode=parse_mode, reply_markup=kb)
        note = "" if res.ok else f" (превью не переслалось: {res.error})"
        return f"💰 цена обновлена: {_money(new_price)} — висит на подтверждении{note}"
    return price_fn


def make_sources_fn(prices_dir):
    """/sources — источники прайсов: имя, позиций, наценка, свежесть. Новый
    источник добавляется просто отправкой .xlsx файлом в чат (бот сам спросит
    наценку); наценка меняется /markup <слот> <±число> (2026-07-07)."""
    def sources_fn() -> str:
        import time as _t
        from content_factory.ingest.excel_price import load_price_slots, get_markups
        slots = load_price_slots(prices_dir)
        if not slots:
            return ("❌ источников нет — пришлите .xlsx прайс файлом в этот чат, "
                    "он добавится источником")
        markups = get_markups(prices_dir)
        lines = ["📦 Источники прайсов:"]
        for label, items in slots:
            pct = markups.get(label, 0)
            pct_s = f" · {'+' if pct > 0 else ''}{pct:g}%" if pct else ""
            p = Path(prices_dir) / f"{label}.xlsx"
            age_h = (_t.time() - p.stat().st_mtime) / 3600 if p.exists() else None
            age_s = f" · {age_h:.0f}ч назад" if age_h is not None else ""
            lines.append(f"• {label}: {len(items)} поз.{pct_s}{age_s}")
        lines.append("\n➕ Добавить: пришлите .xlsx файлом. "
                     "Наценка: /markup <слот> <±число>")
        return "\n".join(lines)
    return sources_fn


_DB_MARKUP_SOURCES = ("breeze", "rusklimat", "daichi", "jac")


def make_markup_fn(prices_dir, state_db=None):
    """markup_fn(slot, pct) — наценка/скидка источника (владелец пишет число
    со знаком: +5 наценка, -7 скидка, 0 убрать). Слот: excel-прайс (файл),
    БД-источник (breeze/rusklimat/daichi/jac) или '*' — дефолт всех БД-источников.
    pct=None — обзор текущих наценок (п.7 владельца 2026-07-09)."""
    def markup_fn(slot: str, pct: float | None) -> str:
        from content_factory.ingest.excel_price import set_markup
        from content_factory.pricing.overrides import (markup_overrides,
                                                       set_markup_override)
        if pct is None:                                # обзор
            ov = markup_overrides(state_db) if state_db else {}
            lines = ["💹 Наценки БД-источников (поверх config.yaml):"]
            if ov:
                lines += [f"— {s}: {'+' if p > 0 else ''}{p:g}%"
                          for s, p in sorted(ov.items())]
            else:
                lines.append("— переопределений нет (действует yaml)")
            lines.append("Менять: /markup breeze -3 · /markup * 8 · 0 — убрать.\n"
                         "Прайсы: /markup <слот из /sources> <±число>")
            return "\n".join(lines)
        s = slot.strip().lower()
        if state_db and (s in _DB_MARKUP_SOURCES or s == "*"):
            set_markup_override(state_db, s, pct if pct else None)
            name = "БД-источников (дефолт *)" if s == "*" else f"«{s}»"
            sign = f"{'+' if pct > 0 else ''}{pct:g}%"
            return (f"💹 наценка {name}: {sign} — применится со следующего тика "
                    f"(посты и синк)" if pct
                    else f"💹 наценка {name} убрана — действует yaml")
        if not (Path(prices_dir) / f"{slot}.xlsx").exists():
            return f"❌ нет такого источника: {slot} (см. /sources)"
        set_markup(prices_dir, slot, pct)
        sign = f"{'+' if pct > 0 else ''}{pct:g}%"
        return (f"💹 наценка «{slot}»: {sign} — применится ко всем ценам источника "
                f"(листинги и превью)" if pct else f"💹 наценка «{slot}» убрана")
    return markup_fn


_EXCEL_ACTIVE_STATUSES = ("new", "research", "card")   # до preview — можно отменить


def make_cancel_excel_fn(state_db, queue_db):
    """cancel_fn(key|'*') → отмена товаров excel-конвейера (запрос владельца
    2026-07-06: «нет кнопки остановить задачу»). Товар → status='cancelled'
    (тик его больше не продвигает), связанные PENDING-задачи очереди агента
    снимаются; processing не трогаем — агент уже генерит, результат просто
    никуда не поедет (товар вне конвейера)."""
    def cancel_fn(target: str) -> str:
        from content_factory.orchestrator.excel_pipeline import ExcelStore
        store = ExcelStore(state_db)
        items = [i for s in _EXCEL_ACTIVE_STATUSES for i in store.by_status(s)]
        if target != "*":
            items = [i for i in items if i.key == target]
        if not items:
            return "❌ нечего отменять — активных задач нет"
        cancelled_jobs = 0
        with sqlite3.connect(queue_db) as q:
            for item in items:
                store.update(item.key, status="cancelled")
                for job_id in (item.research_job, item.card_job):
                    if job_id:
                        cancelled_jobs += q.execute(
                            "UPDATE jobs SET status='cancelled' "
                            "WHERE id=? AND status='pending'", (job_id,)).rowcount
        names = ", ".join(i.name[:30] for i in items[:3])
        more = f" (+{len(items) - 3})" if len(items) > 3 else ""
        return (f"🛑 отменено {len(items)}: {names}{more}"
                + (f"; снято из очереди агента: {cancelled_jobs}" if cancelled_jobs else ""))
    return cancel_fn


def excel_cancel_markup(state_db, links) -> dict | None:
    """Кнопки отмены под /excel-статусом: по одной на активный товар (до 8) +
    «отменить все». None, если в конвейере нет активных. excel-ключи длинные —
    в callback_data короткий код (OrderLinks, как у превью-кнопок)."""
    from content_factory.orchestrator.excel_pipeline import ExcelStore
    store = ExcelStore(state_db)
    items = [i for s in _EXCEL_ACTIVE_STATUSES for i in store.by_status(s)]
    if not items:
        return None
    rows = [[{"text": f"🛑 {i.name[:40]}",
              "callback_data": f"excancel:{links.code_for(i.key)}"}]
            for i in items[:8]]
    rows.append([{"text": f"🛑 Отменить все ({len(items)})",
                  "callback_data": "excancel:*"}])
    return {"inline_keyboard": rows}


def download_telegram_file(http, token: str, file_id: str) -> bytes | None:
    """Скачать файл из Telegram по file_id (getFile → скачивание по file_path).
    None, если Telegram не отдал file_path (файл недоступен/устарел). Общий хелпер
    для .xlsx-прайса (receive_price) и фото/УТП в визарде /task."""
    r = http.get(f"{TG_API}/bot{token}/getFile", params={"file_id": file_id})
    file_path = ((r.json() or {}).get("result") or {}).get("file_path")
    if not file_path:
        return None
    return http.get(f"{TG_API}/file/bot{token}/{file_path}").content


def receive_price(http, token: str, doc: dict, prices_dir, slot: str = "manual") -> str:
    """Скачать .xlsx (от владельца файлом в чат — slot='manual', или из
    канала-поставщика — slot='channel') и сделать его текущим прайсом в своём
    слоте (см. load_price_slots: раздельные слоты — иначе слоты перезаписывали
    бы друг друга)."""
    from content_factory.ingest.excel_price import parse_price_xlsx, manual_slot_name
    data = download_telegram_file(http, token, doc.get("file_id"))
    if data is None:
        return "❌ не удалось скачать файл из Telegram"
    pdir = Path(prices_dir)
    pdir.mkdir(parents=True, exist_ok=True)
    name = doc.get("file_name") or "price.xlsx"
    (pdir / name).write_bytes(data)
    # Ручной прайс — в свой слот поставщика (manual__<из-имени>): не затирает
    # предыдущего поставщика. Канал/почта — свои фиксированные слоты.
    slot_file = manual_slot_name(name) if slot == "manual" else slot
    (pdir / f"{slot_file}.xlsx").write_bytes(data)
    try:
        items = parse_price_xlsx(pdir / f"{slot_file}.xlsx")
    except Exception as e:
        return f"❌ файл сохранён, но не парсится: {e}"
    sections = {}
    for i in items:
        sections[i.section] = sections.get(i.section, 0) + 1
    top = "\n".join(f"— {s}: {n}" for s, n in
                    sorted(sections.items(), key=lambda x: -x[1])[:8])
    n_sup = len(list(pdir.glob("manual__*.xlsx")))
    extra = f"\nПоставщиков в поиске: {n_sup}." if slot == "manual" else ""
    return (f"📎 Прайс «{name}» принят: {len(items)} позиций, {len(sections)} разделов.{extra}\n"
            f"Крупнейшие разделы:\n{top}\n\n"
            f"Дальше: /make 10 холодильники beko=3 stinol=* — и конвейер сделает превью.")


def resolve_callback_data(data: str, confirm_store, links) -> str:
    """Кнопки превью несут короткий код вместо ключа, если ключ длиннее лимита
    Telegram (64 байта callback_data; excel-ключи из прайсов — длинные).
    Разворачиваем код обратно в ключ через OrderLinks."""
    if ":" not in (data or ""):
        return data
    act, payload = data.split(":", 1)
    if confirm_store.get(payload) is None:
        full = links.key_for(payload)
        if full:
            return f"{act}:{full}"
    return data


def finalize_preview(http, token: str, cq: dict, verdict: str) -> None:
    """После ✅/❌ в ревью-канале: заменить кнопки превью одной «вердикт»-кнопкой
    (подпись/форматирование не трогаем — канал остаётся журналом ревью)."""
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    if not (chat_id and message_id):
        return
    kb = json.dumps({"inline_keyboard": [[{"text": verdict[:60], "callback_data": "noop"}]]},
                    ensure_ascii=False)
    try:
        http.post(f"{TG_API}/bot{token}/editMessageReplyMarkup",
                  data={"chat_id": chat_id, "message_id": message_id, "reply_markup": kb})
    except httpx.HTTPError:
        pass


def get_updates(token: str, offset: int, timeout: int = 30, http=None) -> list:
    client = http or httpx.Client(timeout=timeout + 10)
    r = client.get(f"{TG_API}/bot{token}/getUpdates",
                   params={"offset": offset, "timeout": timeout})
    return (r.json() or {}).get("result", [])


def _make_wizard(cfg, owner, prices_dir, http, excel_fn):
    """Собирает визард /task: своя очередь-стор + submit_card (в обход research,
    если владелец дал фото — см. wizard_flow.py) + сохранение фото на диск."""
    from content_factory.bot.wizard import WizardStore
    from content_factory.bot.wizard_flow import make_wizard_flow
    from content_factory.orchestrator.card_submit import make_card_submitter
    api = config("FOTOGEN_API_URL", cfg.fotogen.api_url).rstrip("/")
    headers = {"x-agent-token": config("FOTOGEN_API_TOKEN")}
    output_dir = Path(config("FOTOGEN_OUTPUT_DIR"))
    queue_db = config("FOTOGEN_QUEUE_DB")
    submit_card = make_card_submitter(api, headers, output_dir, owner, queue_db, http=http)
    photos_dir = Path(cfg.state.db).parent / "manual_photos"

    def save_photo(chat_id, photo_bytes):
        photos_dir.mkdir(parents=True, exist_ok=True)
        p = photos_dir / f"wizard_{chat_id}_{int(time.time() * 1000)}.jpg"
        p.write_bytes(photo_bytes)
        return str(p.resolve())    # абсолютный: отн. путь клеился с чужим каталогом

    store = WizardStore(cfg.state.db)
    return make_wizard_flow(cfg.state.db, prices_dir, store, submit_card, save_photo,
                            excel_fn)


def auto_markup(enabled: bool) -> dict:
    """Кнопки под ответами /auto (владелец 2026-07-09: кнопки, не команды):
    контекстный вкл/выкл + редактор расписания (время/кол-во/категории/сброс)."""
    toggle = ({"text": "⏸ Выключить авто-контент", "callback_data": "auto:off"}
              if enabled else
              {"text": "▶️ Включить авто-контент", "callback_data": "auto:on"})
    return {"inline_keyboard": [
        [toggle],
        [{"text": "🕐 Время", "callback_data": "auto:ask:times"},
         {"text": "🔢 Кол-во", "callback_data": "auto:ask:count"},
         {"text": "📦 Категории", "callback_data": "auto:ask:cats"}],
        [{"text": "↩️ Сброс расписания к yaml", "callback_data": "auto:reset"}],
    ]}


def setup_bot_commands(http, token: str, owner: str) -> None:
    """setMyCommands: управляющее меню (/task /make /find …) видит ТОЛЬКО владелец
    (scope chat). У всех остальных — клиентов, пришедших по кнопке «Заказать» —
    меню пустое, чтобы они видели лишь опросник заказа. Best-effort (сеть/ID)."""
    if not (token and owner):
        return
    owner_cmds = [
        {"command": "task", "description": "Поставить задачу кнопками"},
        {"command": "make", "description": "Авто-выбор из прайса по категории"},
        {"command": "find", "description": "Найти позиции в прайсе"},
        {"command": "pick", "description": "Взять номера из /find в работу"},
        {"command": "excel", "description": "Статус конвейера прайса"},
        {"command": "pending", "description": "Посты на подтверждении"},
        {"command": "status", "description": "Что в очереди"},
        {"command": "auto", "description": "Авто-контент: статус, вкл/выкл"},
    ]
    try:
        http.post(f"{TG_API}/bot{token}/setMyCommands",
                  data={"commands": json.dumps(owner_cmds, ensure_ascii=False),
                        "scope": json.dumps({"type": "chat", "chat_id": int(owner)},
                                            ensure_ascii=False)})
        http.post(f"{TG_API}/bot{token}/setMyCommands",
                  data={"commands": "[]",
                        "scope": json.dumps({"type": "default"}, ensure_ascii=False)})
    except (httpx.HTTPError, ValueError):
        pass


def main():
    cfg = load_config(Path("config/config.yaml"))
    token = config("TELEGRAM_BOT_TOKEN")
    owner = str(config("TELEGRAM_OWNER_CHAT_ID", config("FOTOGEN_CHAT_ID", "")))
    q = TaskQueue(cfg.state.db)
    cs = ConfirmStore(cfg.state.db)
    ps = PublishState(cfg.state.db)
    links = OrderLinks(cfg.state.db)
    pending = PendingCmdStore(cfg.state.db)              # ждём аргумент /find /make /pick
    order_store = OrderDialogStore(cfg.state.db)         # опросник заказа клиента
    order_start, order_callback, order_text, order_contact = make_order_flow(
        order_store, links, ps)
    lead_chat = config("TELEGRAM_LEAD_CHAT_ID", owner)   # куда слать лиды (дефолт — владелец)
    price_channel = str(config("TELEGRAM_PRICE_CHANNEL_ID", ""))  # авто-забор daily-прайса
    publish_fn = make_publish_fn(token, cfg.telegram.parse_mode, ps,
                                 order_bot=cfg.telegram.order_bot, links=links)
    regen_fn = make_regen_fn(cfg.state.card_jobs_db, cfg.state.db)
    prices_dir = Path(cfg.state.db).parent / "prices"
    make_fn = make_make_fn(cfg.state.db, prices_dir)
    find_fn, pick_fn, excel_fn = make_find_pick_fns(cfg.state.db, prices_dir)
    cancel_excel_fn = make_cancel_excel_fn(cfg.state.db, config("FOTOGEN_QUEUE_DB"))
    http = httpx.Client(timeout=40)
    review_channel = config("TELEGRAM_REVIEW_CHANNEL_ID", cfg.telegram.review_channel_id)
    price_fn = make_price_fn(cfg.state.db, token, review_channel,
                             cfg.telegram.parse_mode, links, http=http)
    sources_fn = make_sources_fn(prices_dir)
    markup_fn = make_markup_fn(prices_dir, cfg.state.db)

    # /auto: выключатель автомата (флаг в state-БД, слоты в общей очереди q)
    def cats_catalog_fn():
        """id→название категорий склада (для /auto cats словами). БД недоступна
        (локальный запуск без туннеля) → None: резолв только по числам."""
        try:
            import psycopg2
            conn = psycopg2.connect(
                host=config("DB_HOST", "localhost"), port=config("DB_PORT", "5432"),
                dbname=config("DB_NAME"), user=config("DB_USER"),
                password=config("DB_PASSWORD"), connect_timeout=5)
            cur = conn.cursor()
            cur.execute("SELECT c.id, c.title FROM catalog_category c "
                        "JOIN catalog_product p ON p.category_id = c.id "
                        "GROUP BY c.id, c.title")
            rows = dict(cur.fetchall())
            conn.close()
            return rows
        except Exception:                          # noqa: BLE001 — мягкий фолбэк
            return None

    def auto_fn(arg):
        return auto_command(arg, cfg.auto_tasks, q, cfg.state.db, datetime.now(),
                            catalog_fn=cats_catalog_fn)

    def auto_state_fn():
        return auto_enabled(cfg.state.db) if cfg.auto_tasks else None
    wizard_start, wizard_text, wizard_photo, wizard_callback = _make_wizard(
        cfg, owner, prices_dir, http, excel_fn)

    def _wizard_safe(fn, *args):
        """Визард не должен ронять бота одним апдейтом: crash-loop 2026-07-09 —
        FileNotFoundError на confirm валил main(), systemd рестартил, offset не
        сдвигался и тот же callback падал снова (31 рестарт). Ошибка → трейс в
        журнал + мягкий ответ владельцу."""
        try:
            return fn(*args)
        except Exception:                              # noqa: BLE001
            import traceback
            traceback.print_exc()
            from content_factory.bot.wizard_flow import WizardReply
            return WizardReply("⚠️ внутренняя ошибка — попробуйте ещё раз "
                               "(детали в журнале cf-bot)")

    def _send_wizard_reply(chat_id, wr):
        data = {"chat_id": chat_id, "text": wr.text}
        if wr.markup:
            data["reply_markup"] = json.dumps(wr.markup, ensure_ascii=False)
        try:
            http.post(f"{TG_API}/bot{token}/sendMessage", data=data)
        except httpx.HTTPError:
            pass

    def _send_force_reply(chat_id, text, placeholder):
        """Приглашение с активным полем ввода (ForceReply) — для пустых /find /make /pick."""
        markup = json.dumps({"force_reply": True, "input_field_placeholder": placeholder},
                            ensure_ascii=False)
        try:
            http.post(f"{TG_API}/bot{token}/sendMessage",
                      data={"chat_id": chat_id, "text": text, "reply_markup": markup})
        except httpx.HTTPError:
            pass

    def _send_order_reply(chat_id, r):
        """Ответ клиенту в опроснике заказа. Если заявка готова (r.lead) — шлём её
        в чат лидов (TELEGRAM_LEAD_CHAT_ID), а не смешиваем с управляющим ботом."""
        data = {"chat_id": chat_id, "text": r.text}
        if r.markup:
            data["reply_markup"] = json.dumps(r.markup, ensure_ascii=False)
        elif r.keyboard:
            data["reply_markup"] = json.dumps(r.keyboard, ensure_ascii=False)
        elif r.force_reply:
            data["reply_markup"] = json.dumps(
                {"force_reply": True, "input_field_placeholder": r.placeholder or ""},
                ensure_ascii=False)
        try:
            http.post(f"{TG_API}/bot{token}/sendMessage", data=data)
        except httpx.HTTPError:
            pass
        if r.lead and lead_chat:
            try:
                http.post(f"{TG_API}/bot{token}/sendMessage",
                          data={"chat_id": lead_chat, "text": r.lead})
            except httpx.HTTPError:
                pass

    setup_bot_commands(http, token, owner)   # управляющее меню — только владельцу
    offset = 0
    print("bot: long-poll запущен")
    while True:
        try:
            updates = get_updates(token, offset, http=http)
        except httpx.HTTPError:
            time.sleep(3)
            continue
        for u in updates:
            offset = u["update_id"] + 1

            # --- Нажатие inline-кнопки (✅/❌ под превью) ---
            cq = u.get("callback_query")
            if cq:
                data_cq = cq.get("data") or ""
                # Опросник заказа: кнопки кол-ва/пропуска от ЛЮБОГО клиента — до owner-гейта.
                if data_cq.startswith("order:"):
                    chat_o = str((cq.get("message") or {}).get("chat", {}).get("id", ""))
                    r = order_callback(chat_o, data_cq, cq.get("from") or {})
                    try:
                        http.post(f"{TG_API}/bot{token}/answerCallbackQuery",
                                  data={"callback_query_id": cq.get("id"), "text": r.text[:180]})
                    except httpx.HTTPError:
                        pass
                    _send_order_reply(chat_o, r)
                    continue
                frm = str((cq.get("from") or {}).get("id", ""))
                if owner and frm != owner:
                    continue
                if cq.get("data") == "noop":          # «вердикт»-кнопка уже нажатого превью
                    try:
                        http.post(f"{TG_API}/bot{token}/answerCallbackQuery",
                                  data={"callback_query_id": cq.get("id")})
                    except httpx.HTTPError:
                        pass
                    continue
                if (cq.get("data") or "").startswith("wizard:"):
                    chat_w = str((cq.get("message") or {}).get("chat", {}).get("id", ""))
                    wr = _wizard_safe(wizard_callback, chat_w, cq.get("data"))
                    try:
                        http.post(f"{TG_API}/bot{token}/answerCallbackQuery",
                                  data={"callback_query_id": cq.get("id"), "text": wr.text[:180]})
                    except httpx.HTTPError:
                        pass
                    _send_wizard_reply(chat_w, wr)
                    continue
                if data_cq.startswith("price:"):       # «💰 Изменить цену» на превью
                    code = data_cq.split(":", 1)[1]
                    key_p = links.key_for(code) or code
                    chat_p = str((cq.get("message") or {}).get("chat", {}).get("id", ""))
                    # ответ владельца станет «/price <key> <цена>» (resolve_reply)
                    pending.set(owner or chat_p, f"/price {key_p}")
                    try:
                        http.post(f"{TG_API}/bot{token}/answerCallbackQuery",
                                  data={"callback_query_id": cq.get("id")})
                    except httpx.HTTPError:
                        pass
                    _send_force_reply(owner or chat_p,
                                      f"💰 Новая цена для «{key_p[:50]}»? Только число.",
                                      "напр.: 25990")
                    continue
                if data_cq.startswith("excancel:"):    # отмена задач excel-конвейера
                    target = data_cq.split(":", 1)[1]
                    if target != "*":
                        target = links.key_for(target) or target
                    reply = cancel_excel_fn(target)
                    chat_c = str((cq.get("message") or {}).get("chat", {}).get("id", ""))
                    try:
                        http.post(f"{TG_API}/bot{token}/answerCallbackQuery",
                                  data={"callback_query_id": cq.get("id"), "text": reply[:180]})
                        http.post(f"{TG_API}/bot{token}/sendMessage",
                                  data={"chat_id": chat_c, "text": reply})
                    except httpx.HTTPError:
                        pass
                    continue
                if data_cq.startswith("auto:ask:"):    # редактор — спросить значение
                    what = data_cq.rsplit(":", 1)[1]
                    prompts = {
                        "times": ("🕐 Времена слотов через запятую", "09:00, 13:00, 18:00"),
                        "count": ("🔢 Сколько серий на слот — число", "2"),
                        "cats": ("📦 Категории склада словами (или id) через запятую",
                                 "сплит-системы, мобильные кондиционеры")}
                    label, ph = prompts.get(what, ("Значение", ""))
                    chat_a = str((cq.get("message") or {}).get("chat", {}).get("id", ""))
                    # ответ владельца станет «/auto times …» (паттерн кнопки price:)
                    pending.set(owner or chat_a, f"/auto {what}")
                    try:
                        http.post(f"{TG_API}/bot{token}/answerCallbackQuery",
                                  data={"callback_query_id": cq.get("id")})
                    except httpx.HTTPError:
                        pass
                    _send_force_reply(owner or chat_a, f"{label}?", ph)
                    continue
                if data_cq.startswith("auto:"):        # кнопки вкл/выкл/сброс автомата
                    reply = auto_fn(data_cq.split(":", 1)[1])
                    chat_a = str((cq.get("message") or {}).get("chat", {}).get("id", ""))
                    d = {"chat_id": chat_a, "text": reply}
                    st_a = auto_state_fn()
                    if st_a is not None:               # свежая кнопка под новым статусом
                        d["reply_markup"] = json.dumps(auto_markup(st_a), ensure_ascii=False)
                    try:
                        http.post(f"{TG_API}/bot{token}/answerCallbackQuery",
                                  data={"callback_query_id": cq.get("id"), "text": reply[:180]})
                        http.post(f"{TG_API}/bot{token}/sendMessage", data=d)
                    except httpx.HTTPError:
                        pass
                    continue
                reply = handle_callback(resolve_callback_data(cq.get("data", ""), cs, links),
                                        q, confirm_store=cs,
                                        publish_fn=publish_fn, publish_state=ps,
                                        regen_fn=regen_fn)
                try:
                    http.post(f"{TG_API}/bot{token}/answerCallbackQuery",
                              data={"callback_query_id": cq.get("id"), "text": reply[:180]})
                except httpx.HTTPError:
                    pass
                # вместо эха в чат — приписываем вердикт к самому превью (журнал ревью)
                finalize_preview(http, token, cq, reply)
                continue

            # Прайс из канала-поставщика (бот там админ, .xlsx прилетает сам —
            # без участия владельца): свой слот channel.xlsx, сводка владельцу в личку.
            cpost = u.get("channel_post") or u.get("edited_channel_post")
            if cpost and price_channel and str((cpost.get("chat") or {}).get("id", "")) == price_channel:
                cdoc = cpost.get("document") or {}
                if (cdoc.get("file_name") or "").lower().endswith(".xlsx"):
                    reply = receive_price(http, token, cdoc, prices_dir, slot="channel")
                    try:
                        http.post(f"{TG_API}/bot{token}/sendMessage",
                                 data={"chat_id": owner, "text": f"📡 Из канала: {reply}"})
                    except httpx.HTTPError:
                        pass
                continue

            # отредактированное сообщение — тоже команда (владелец часто правит опечатку)
            msg = u.get("message") or u.get("edited_message") or {}
            chat = str((msg.get("chat") or {}).get("id", ""))
            text = msg.get("text", "")
            # Голосовое сообщение — распознаём в текст (ffmpeg+Google Speech Recognition,
            # см. bot/voice.py) и дальше обрабатываем как обычный текст (визард /task,
            # /make и т.д.). Модели/артикулы речь распознаёт ненадёжно — предупреждаем.
            voice = msg.get("voice") or {}
            if voice and (not owner or chat == owner) and not text:
                # Длинные голосовые распознаются кусками (несколько сетевых
                # round-trip'ов) — без этого сообщения выглядело бы зависшим.
                try:
                    http.post(f"{TG_API}/bot{token}/sendMessage",
                             data={"chat_id": chat, "text": "🎤 Распознаю голосовое…"})
                except httpx.HTTPError:
                    pass
                data = download_telegram_file(http, token, voice.get("file_id"))
                if data is None:
                    continue
                try:
                    text = transcribe_voice_bytes(data)
                except Exception as e:
                    try:
                        http.post(f"{TG_API}/bot{token}/sendMessage",
                                 data={"chat_id": chat, "text": f"❌ Не распознал голосовое: {e}"})
                    except httpx.HTTPError:
                        pass
                    continue
                try:
                    http.post(f"{TG_API}/bot{token}/sendMessage",
                             data={"chat_id": chat, "text": f"🎤 Я услышал: «{text}»"})
                except httpx.HTTPError:
                    pass
            # Заказ по кнопке из канала: /start ord_<code> запускает опросник заказа
            # (разрешён ЛЮБОМУ пользователю; всё остальное от чужих игнорируется).
            if text.startswith("/start ord_"):
                code = text.split(maxsplit=1)[1][4:].strip()   # "ord_<code>" → "<code>"
                _send_order_reply(chat, order_start(chat, code, msg.get("from") or {}))
                continue
            # Excel-прайс файлом (только от владельца)
            doc = msg.get("document") or {}
            if (doc and (not owner or chat == owner)
                    and (doc.get("file_name") or "").lower().endswith(".xlsx")):
                reply = receive_price(http, token, doc, prices_dir)
                try:
                    http.post(f"{TG_API}/bot{token}/sendMessage",
                              data={"chat_id": chat, "text": reply})
                except httpx.HTTPError:
                    pass
                # Источник добавлен — следующим шагом спрашиваем наценку
                # (ответ владельца станет «/markup <слот> <число>» через resolve_reply)
                if reply.startswith("📎"):
                    from content_factory.ingest.excel_price import manual_slot_name
                    slot = manual_slot_name(doc.get("file_name") or "price.xlsx")
                    pending.set(chat, f"/markup {slot}")
                    _send_force_reply(chat,
                                      f"💹 Наценка для «{slot}»? Число со знаком: "
                                      f"+5 (наценка), -7 (скидка), 0 (без).",
                                      "напр.: +5")
                continue
            # Клиент поделился телефоном (request_contact) в опроснике заказа.
            contact = msg.get("contact")
            if contact:
                r = order_contact(chat, contact.get("phone_number", ""),
                                  msg.get("from") or {})
                if r is not None:
                    _send_order_reply(chat, r)
                    continue
            # Комментарий / своё количество / телефон-текст в опроснике заказа —
            # от ЛЮБОГО клиента, но только при активном диалоге (иначе order_text → None).
            if text:
                r = order_text(chat, text, msg.get("from") or {})
                if r is not None:
                    _send_order_reply(chat, r)
                    continue
            if owner and chat != owner:                    # только владелец управляет ботом
                continue
            # /task — старт визарда постановки задачи кнопками
            if text.strip() == "/task":
                _send_wizard_reply(chat, _wizard_safe(wizard_start, chat))
                continue
            # фото в визарде (шаг «приложить фото» — последний элемент = крупнее всех)
            photos = msg.get("photo") or []
            if photos:
                data = download_telegram_file(http, token, photos[-1].get("file_id"))
                wr = _wizard_safe(wizard_photo, chat, data) if data is not None else None
                if wr is not None:
                    _send_wizard_reply(chat, wr)
                    continue
            # текстовый шаг визарда (категория/список/УТП) — если диалог активен
            if text:
                wr = _wizard_safe(wizard_text, chat, text)
                if wr is not None:
                    _send_wizard_reply(chat, wr)
                    continue
            # Пустой вызов /find /make /pick → приглашение с полем ввода (ForceReply);
            # следующий текст (не команда) подставляется как аргумент этой команды.
            if text:
                bc = bare_arg_command(text)
                if bc:
                    pending.set(chat, bc)
                    _send_force_reply(chat, *prompt_for(bc))
                    continue
                reconstructed = resolve_reply(pending.take(chat), text)
                if reconstructed:
                    text = reconstructed
            if not text:
                continue
            reply = handle_command(text, q, confirm_store=cs, publish_fn=publish_fn,
                                   publish_state=ps, regen_fn=regen_fn, make_fn=make_fn,
                                   find_fn=find_fn, pick_fn=pick_fn, excel_fn=excel_fn,
                                   price_fn=price_fn, sources_fn=sources_fn,
                                   markup_fn=markup_fn, auto_fn=auto_fn,
                                   auto_state_fn=auto_state_fn)
            data = {"chat_id": chat, "text": reply}
            if text.strip().startswith("/excel"):      # кнопки отмены активных задач
                markup = excel_cancel_markup(cfg.state.db, links)
                if markup:
                    data["reply_markup"] = json.dumps(markup, ensure_ascii=False)
            if text.strip().startswith(("/auto", "/status")):   # кнопка вкл/выкл автомата
                st_a = auto_state_fn()
                if st_a is not None:
                    data["reply_markup"] = json.dumps(auto_markup(st_a), ensure_ascii=False)
            try:
                http.post(f"{TG_API}/bot{token}/sendMessage", data=data)
            except httpx.HTTPError:
                pass


if __name__ == "__main__":
    main()
