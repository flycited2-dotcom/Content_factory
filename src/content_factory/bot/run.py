"""Telegram-бот (long-poll). Принимает команды ТОЛЬКО от владельца (chat_id из .env),
маршрутизирует через bot.commands.handle_command. Команда /approve публикует подтверждённый
пост в канал (confirm-пилот). Запуск как сервис `cf-bot`.

  python -m content_factory.bot.run
"""
from __future__ import annotations
import json
import sqlite3
import time
from pathlib import Path
import httpx
from decouple import config

from content_factory.config import load_config
from content_factory.publish.telegram import publish_post, PublishState, TG_API
from content_factory.publish.orders import OrderLinks, order_markup, handle_order_start
from content_factory.orchestrator.queue import TaskQueue
from content_factory.orchestrator.confirm_store import ConfirmStore
from content_factory.bot.commands import handle_command, handle_callback


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


def make_regen_fn(card_jobs_db):
    """regen_fn(awaiting) → убрать карточку для перегенерации: удалить файл и запись
    CardJobStore (ключ store = имя файла карточки без расширения). После этого серия
    снова «без карточки» — cf-cards пересабмитит её агенту на ближайшем тике."""
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
        return True
    return regen_fn


def make_make_fn(state_db, prices_dir):
    """make_fn(count, category, quotas) для /make: выбрать позиции из последнего
    прайса и поставить в excel-конвейер (research → карточка → превью)."""
    def make_fn(count, category, quotas):
        from content_factory.ingest.excel_price import (
            parse_price_xlsx, select_from_price, item_key, extract_model)
        from content_factory.orchestrator.excel_pipeline import ExcelStore
        from content_factory.orchestrator.confirm_store import ConfirmStore
        latest = Path(prices_dir) / "latest.xlsx"
        if not latest.exists():
            return "❌ прайс не загружен — пришлите .xlsx файлом в этот чат"
        items = parse_price_xlsx(latest)
        store = ExcelStore(state_db)
        taken = (PublishState(state_db).published_keys()
                 | ConfirmStore(state_db).blocked_keys() | store.all_keys())
        got = select_from_price(items, category, quotas, count, taken)
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
        parse_price_xlsx, search_items, item_key, extract_model)
    from content_factory.orchestrator.excel_pipeline import ExcelStore
    from content_factory.orchestrator.confirm_store import ConfirmStore

    def _taken(store):
        return (PublishState(state_db).published_keys()
                | ConfirmStore(state_db).blocked_keys() | store.all_keys())

    def _pick_table(c):
        c.execute("CREATE TABLE IF NOT EXISTS pick_list (idx INTEGER PRIMARY KEY, "
                  "key TEXT, brand TEXT, model TEXT, name TEXT, price INTEGER)")

    def find_fn(phrase):
        latest = Path(prices_dir) / "latest.xlsx"
        if not latest.exists():
            return "❌ прайс не загружен — пришлите .xlsx файлом"
        store = ExcelStore(state_db)
        found = search_items(parse_price_xlsx(latest), phrase, _taken(store), limit=15)
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

    def excel_fn():
        store = ExcelStore(state_db)
        counts = {s: len(store.by_status(s))
                  for s in ("new", "research", "card", "preview", "failed")}
        lines = [f"Конвейер прайса: 🆕 {counts['new']} · 🔎 research {counts['research']} · "
                 f"🎨 card {counts['card']} · 👀 превью {counts['preview']} · "
                 f"❌ failed {counts['failed']}"]
        for s, mark in (("research", "🔎"), ("card", "🎨"), ("failed", "❌")):
            for i in store.by_status(s)[:5]:
                extra = f" ({i.error})" if s == "failed" and i.error else ""
                lines.append(f"{mark} {i.brand} {i.model}{extra}".strip())
        return "\n".join(lines)

    return find_fn, pick_fn, excel_fn


def receive_price(http, token: str, doc: dict, prices_dir) -> str:
    """Скачать присланный владельцем .xlsx и сделать его текущим прайсом."""
    from content_factory.ingest.excel_price import parse_price_xlsx
    r = http.get(f"{TG_API}/bot{token}/getFile", params={"file_id": doc.get("file_id")})
    file_path = ((r.json() or {}).get("result") or {}).get("file_path")
    if not file_path:
        return "❌ не удалось скачать файл из Telegram"
    data = http.get(f"{TG_API}/file/bot{token}/{file_path}").content
    pdir = Path(prices_dir)
    pdir.mkdir(parents=True, exist_ok=True)
    name = doc.get("file_name") or "price.xlsx"
    (pdir / name).write_bytes(data)
    (pdir / "latest.xlsx").write_bytes(data)
    try:
        items = parse_price_xlsx(pdir / "latest.xlsx")
    except Exception as e:
        return f"❌ файл сохранён, но не парсится: {e}"
    sections = {}
    for i in items:
        sections[i.section] = sections.get(i.section, 0) + 1
    top = "\n".join(f"— {s}: {n}" for s, n in
                    sorted(sections.items(), key=lambda x: -x[1])[:8])
    return (f"📎 Прайс «{name}» принят: {len(items)} позиций, {len(sections)} разделов.\n"
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


def main():
    cfg = load_config(Path("config/config.yaml"))
    token = config("TELEGRAM_BOT_TOKEN")
    owner = str(config("TELEGRAM_OWNER_CHAT_ID", config("FOTOGEN_CHAT_ID", "")))
    q = TaskQueue(cfg.state.db)
    cs = ConfirmStore(cfg.state.db)
    ps = PublishState(cfg.state.db)
    links = OrderLinks(cfg.state.db)
    lead_chat = config("TELEGRAM_LEAD_CHAT_ID", owner)   # куда слать лиды (дефолт — владелец)
    publish_fn = make_publish_fn(token, cfg.telegram.parse_mode, ps,
                                 order_bot=cfg.telegram.order_bot, links=links)
    regen_fn = make_regen_fn(cfg.state.card_jobs_db)
    prices_dir = Path(cfg.state.db).parent / "prices"
    make_fn = make_make_fn(cfg.state.db, prices_dir)
    find_fn, pick_fn, excel_fn = make_find_pick_fns(cfg.state.db, prices_dir)
    http = httpx.Client(timeout=40)
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

            # отредактированное сообщение — тоже команда (владелец часто правит опечатку)
            msg = u.get("message") or u.get("edited_message") or {}
            chat = str((msg.get("chat") or {}).get("id", ""))
            text = msg.get("text", "")
            # Заказ по кнопке из канала: /start ord_<code> разрешён ЛЮБОМУ пользователю
            # (всё остальное от чужих игнорируется, как раньше).
            if text.startswith("/start ord_"):
                reply_c, lead = handle_order_start(text, msg.get("from") or {}, links, ps)
                try:
                    if reply_c:
                        http.post(f"{TG_API}/bot{token}/sendMessage",
                                  data={"chat_id": chat, "text": reply_c})
                    if lead and lead_chat:
                        http.post(f"{TG_API}/bot{token}/sendMessage",
                                  data={"chat_id": lead_chat, "text": lead})
                except httpx.HTTPError:
                    pass
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
                continue
            if not text or (owner and chat != owner):     # только владелец управляет ботом
                continue
            reply = handle_command(text, q, confirm_store=cs, publish_fn=publish_fn,
                                   publish_state=ps, regen_fn=regen_fn, make_fn=make_fn,
                                   find_fn=find_fn, pick_fn=pick_fn, excel_fn=excel_fn)
            try:
                http.post(f"{TG_API}/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": reply})
            except httpx.HTTPError:
                pass


if __name__ == "__main__":
    main()
