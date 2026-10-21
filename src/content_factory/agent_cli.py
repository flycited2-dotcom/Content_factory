"""JSON-интерфейс постановки задач для оркестратора Agents_MY (этап 2).

Оркестратор (Telegram-бот + Claude) не пишет в базу сам: он вызывает эти
подкоманды, а они используют тот же код, что /make, /find и кнопка «Отменить»
в боте. Вывод — одна строка JSON; ошибка входа — {"error": ...} и код 2.

    python -m content_factory.agent_cli hint
    python -m content_factory.agent_cli resolve --category стабилизаторы [--count N]
        [--quotas '{"ресанта": null}'] [--source manual__resanta] [--btu 9-12] [--min-temp 20]
    python -m content_factory.agent_cli enqueue ...те же... [--due "сегодня 12:00"]
    python -m content_factory.agent_cli cancel --keys k1,k2
    python -m content_factory.agent_cli status [--since UNIX]
    python -m content_factory.agent_cli workers        # генерация, дорожки фото-агента, команды сторожам
    python -m content_factory.agent_cli start-workers  # включить генерацию, сторожам 'start', разбудить cf-excel
    python -m content_factory.agent_cli visual-submit --scene "..." --request-key orch-<id>  # картинка фото-агентом
    python -m content_factory.agent_cli visual-status --job N
    python -m content_factory.agent_cli publish-tg --key <id> --caption-file F --image P  # пост оркестратора в канал
    python -m content_factory.agent_cli plan-vk --key <id> --caption-file F --image P     # одобренным в ВК-план

Квота {"бренд": null} без "*" — «все позиции только этого бренда».
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from decouple import config

_ALL = 10 ** 9
LANE_FRESH = 120                     # как в vps_api фото-агента: дорожка жива, если отмечалась за 2 минуты
AGENT_KEYS = ("agent_command", "agent_command_laptop", "agent_command_desktop")


class _InputError(ValueError):
    pass


def _paths(args):
    if args.state_db is None or args.prices_dir is None:
        from content_factory.config import load_config
        cfg = load_config(Path("config/config.yaml"))
        state_db = args.state_db or cfg.state.db
        prices_dir = args.prices_dir or str(Path(cfg.state.db).parent / "prices")
    else:
        state_db, prices_dir = args.state_db, args.prices_dir
    queue_db = args.queue_db if args.queue_db is not None else config("FOTOGEN_QUEUE_DB", default="")
    return state_db, prices_dir, queue_db


def _items(prices_dir, source=None):
    from content_factory.ingest.excel_price import load_price_slots
    slots = load_price_slots(prices_dir, for_telegram=True)
    if source:
        slots = [(label, its) for label, its in slots if label == source]
    labelled = [(label, item) for label, its in slots for item in its]
    return slots, labelled


def _taken(state_db, queue_db):
    from content_factory.orchestrator.confirm_store import ConfirmStore
    from content_factory.orchestrator.excel_pipeline import ExcelStore
    from content_factory.publish.telegram import PublishState
    return ExcelStore(state_db).selection_taken(
        PublishState(state_db).published_keys() | ConfirmStore(state_db).blocked_keys(), queue_db)


def _quotas(raw):
    if not raw:
        return {}
    try:
        quotas = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _InputError(f"квоты не JSON: {exc}") from exc
    if not isinstance(quotas, dict):
        raise _InputError("квоты должны быть JSON-объектом")
    return {str(k).lower(): v for k, v in quotas.items()}


def _select(args, state_db, prices_dir, queue_db):
    """(total_matches, выбранные позиции с источником) — без побочных эффектов."""
    from content_factory.ingest.excel_price import (
        load_search_aliases, search_items, select_from_price)
    _, labelled = _items(prices_dir, args.source)
    labelled = _spec_filter(labelled, args, prices_dir)
    quotas = _quotas(args.quotas)
    brand_all = [b for b, n in quotas.items() if n is None and b != "*"]
    if brand_all and "*" not in quotas:
        # «все позиции бренда»: бренды с null — явные квоты «без предела», числовые квоты
        # сохраняются, добора чужими брендами нет
        named = [b for b in quotas if b != "*"]
        labelled = [(label, i) for label, i in labelled
                    if any(b in f"{i.brand} {i.name}".lower() for b in named)]
        quotas = {b: (_ALL if n is None else n) for b, n in quotas.items()}
    items = [i for _, i in labelled]
    source_of = {id(i): label for label, i in labelled}
    taken = _taken(state_db, queue_db)
    aliases = load_search_aliases(Path("config/search_aliases.yaml"))
    total = len(search_items(items, args.category, taken, limit=_ALL, aliases=aliases))
    count = args.count if args.count is not None else _ALL
    chosen = select_from_price(items, args.category, quotas, count, taken, aliases=aliases)
    return total, [(source_of.get(id(i), ""), i) for i in chosen]


def _btu_range(raw):
    """«9-12» / «9,12» / «12» → (мин, макс) типоразмера kBTU."""
    nums = [int(n) for n in re.findall(r"\d+", raw or "")]
    if not nums:
        raise _InputError(f"не понял мощность: {raw}")
    return min(nums), max(nums)


def _spec_filter(labelled, args, prices_dir):
    """Отбор по мощности и морозостойкости (ac_specs). Нераспознанное под фильтр не попадает."""
    btu, frost = getattr(args, "btu", None), getattr(args, "min_temp", None)
    if not btu and frost is None:
        return labelled
    from content_factory.ingest.ac_specs import min_outdoor_temp, size_kbtu
    from content_factory.ingest.splithub_catalog import read_snapshot
    utp = {e["sku"]: e.get("utp") or "" for e in read_snapshot(prices_dir)} if frost is not None else {}
    lo, hi = _btu_range(btu) if btu else (0, 10 ** 6)
    out = []
    for label, item in labelled:
        size = size_kbtu(item.name, item.article)
        if btu and (size is None or not lo <= size <= hi):
            continue
        if frost is not None:
            temp = min_outdoor_temp(utp.get(item.article, ""))
            if temp is None or temp < frost:
                continue
        out.append((label, item))
    return out


def _item_json(source, item):
    from content_factory.ingest.excel_price import extract_model, item_key
    return {"key": item_key(item), "brand": item.brand, "model": extract_model(item.name, item.brand),
            "name": item.name, "price": item.price, "source": source}


def cmd_hint(args):
    _, prices_dir, _ = _paths(args)
    slots, labelled = _items(prices_dir)
    brands = Counter(i.brand for _, i in labelled if i.brand)
    return {"brands": [b for b, _ in brands.most_common(60)], "sources": [label for label, _ in slots]}


def cmd_resolve(args):
    state_db, prices_dir, queue_db = _paths(args)
    total, chosen = _select(args, state_db, prices_dir, queue_db)
    return {"total_matches": total, "items": [_item_json(s, i) for s, i in chosen]}


def cmd_enqueue(args):
    from content_factory.bot.commands import parse_due_at
    from content_factory.ingest.excel_price import extract_model, item_key
    from content_factory.orchestrator.excel_pipeline import ExcelStore
    state_db, prices_dir, queue_db = _paths(args)
    due_at = None
    if args.due:
        due_at = parse_due_at(args.due, datetime.now())
        if due_at is None:
            raise _InputError(f"не понял время: {args.due}")
    _, chosen = _select(args, state_db, prices_dir, queue_db)
    rows = [(item_key(i), i.brand, extract_model(i.name, i.brand), i.name, i.price) for _, i in chosen]
    accepted = ExcelStore(state_db).select_items(rows, due_at=due_at, queue_db=queue_db) if rows else []
    accepted_keys = [row[0] for row in accepted]
    if accepted_keys and not args.no_wake:
        from content_factory.bot import worker_control
        from content_factory.cards_pipeline import wake_agent
        try:
            worker_control.request_run()
        except Exception:
            pass                                   # таймер конвейера подхватит сам
        if queue_db:
            try:
                wake_agent(queue_db)               # 'wake' поднимает упавшие дорожки, ручной стоп не перебивает
            except sqlite3.Error:
                pass
    return {"requested": len(chosen), "accepted": accepted_keys,
            "items": [_item_json(s, i) for s, i in chosen if item_key(i) in set(accepted_keys)],
            "due_at": due_at}


def cmd_cancel(args):
    from content_factory.bot.run import make_cancel_excel_fn
    from content_factory.orchestrator.confirm_store import ConfirmStore
    from content_factory.orchestrator.excel_pipeline import ExcelStore
    state_db, _, queue_db = _paths(args)
    keys = [k for k in (args.keys or "").split(",") if k]
    if not keys:
        raise _InputError("не указаны ключи")
    store = ExcelStore(state_db)
    before = {k for k in keys if (item := store.get(k)) is not None and item.status != "cancelled"}
    cancel_fn = make_cancel_excel_fn(state_db, queue_db)
    texts = [cancel_fn(k) for k in keys]
    after = {k for k in keys if (item := store.get(k)) is not None and item.status == "cancelled"}
    cancelled = before & after
    awaiting = ConfirmStore(state_db)

    def _status(k):
        item = store.get(k)
        if item is None:
            return "missing"
        decided = awaiting.get(k) if item.status == "preview" else None
        # превью, по которому владелец уже нажал ❌/✅, — не «ждёт решения»
        return decided.status if decided and decided.status in ("rejected", "published") else item.status

    not_cancelled = [{"key": k, "status": _status(k)} for k in keys if k not in cancelled]
    return {"cancelled": len(cancelled), "not_cancelled": not_cancelled, "text": "\n".join(t for t in texts if t)}


def cmd_status(args):
    state_db, _, _ = _paths(args)
    since = args.since or 0.0
    by_status: dict[str, int] = {}
    out: dict = {"by_status": by_status}
    with sqlite3.connect(state_db) as db:
        try:
            for status, n in db.execute("SELECT status, COUNT(*) FROM excel_items GROUP BY status"):
                by_status[status] = n
        except sqlite3.OperationalError:
            pass                                   # конвейер ещё не создавал таблицу
        try:
            out["published_since"] = db.execute(
                "SELECT COUNT(*) FROM published WHERE ts >= ?", (since,)).fetchone()[0]
        except sqlite3.OperationalError:
            pass
    return out


def cmd_workers(args):
    from content_factory.orchestrator.generation import generation_enabled
    state_db, _, queue_db = _paths(args)
    now = datetime.now()
    out = {"generation": generation_enabled(state_db), "lanes": [], "agent_online": False,
           "last_seen": None, "commands": {}}
    if queue_db and Path(queue_db).exists():
        with sqlite3.connect(queue_db, timeout=10) as db:
            try:
                rows = db.execute("SELECT lane, seen_at FROM lane_heartbeat ORDER BY lane").fetchall()
            except sqlite3.OperationalError:
                rows = []
            try:
                out["commands"] = dict(db.execute(
                    "SELECT key, value FROM flags WHERE key LIKE 'agent_command%'").fetchall())
            except sqlite3.OperationalError:
                pass
        for lane, seen_at in rows:
            try:
                ago = (now - datetime.fromisoformat(seen_at)).total_seconds()
            except (TypeError, ValueError):
                continue
            out["lanes"].append({"lane": lane, "seen_at": seen_at, "online": ago <= LANE_FRESH})
        if out["lanes"]:
            out["agent_online"] = any(l["online"] for l in out["lanes"])
            out["last_seen"] = max(l["seen_at"] for l in out["lanes"])
    try:
        from content_factory.bot import worker_control
        out["worker"] = worker_control.status_lines()
    except Exception:
        out["worker"] = []
    return out


def cmd_start_workers(args):
    from content_factory.bot import worker_control
    from content_factory.orchestrator.generation import set_generation_enabled
    state_db, _, queue_db = _paths(args)
    set_generation_enabled(state_db, True)
    commanded = []
    if queue_db:
        with sqlite3.connect(queue_db, timeout=10) as db:
            db.execute("CREATE TABLE IF NOT EXISTS flags (key TEXT PRIMARY KEY, value TEXT)")
            for key in AGENT_KEYS:                  # явное решение владельца — 'start' перебивает ручной стоп
                db.execute("INSERT INTO flags (key, value) VALUES (?, 'start') "
                           "ON CONFLICT(key) DO UPDATE SET value='start'", (key,))
                commanded.append(key)
    try:
        requested = bool(worker_control.request_run())
    except Exception:
        requested = False
    return {"generation": True, "commanded": commanded, "requested_run": requested}


# ── Этап 3: редакционные посты оркестратора (картинка, публикация по ✅ владельца) ──

_VISUAL_DEAD = ("failed", "cancelled", "error")


def _fotogen_submit(api: str, token: str, data: dict) -> int:
    import httpx
    r = httpx.post(f"{api.rstrip('/')}/api/submit-research", headers={"x-agent-token": token},
                   data=data, timeout=60)
    r.raise_for_status()
    return int(r.json()["job_id"])


def _queue_row(queue_db, sql, params):
    if not queue_db or not Path(queue_db).is_file():
        return None
    with sqlite3.connect(Path(queue_db).resolve().as_uri() + "?mode=ro", uri=True, timeout=10) as db:
        return db.execute(sql, params).fetchone()


def cmd_visual_submit(args):
    """Картинка к посту — фото-агентом, как редакционные визуалы ВК (scripts/submit_missing_visuals.py).
    Повтор с тем же request_key возвращает живое задание, а не заказывает второе."""
    _, _, queue_db = _paths(args)
    if not args.new:
        row = _queue_row(queue_db, "SELECT id, status FROM jobs WHERE request_key=? ORDER BY id DESC LIMIT 1",
                         (args.request_key,))
        if row and row[1] not in _VISUAL_DEAD:
            return {"job_id": int(row[0]), "reused": True}
    api = config("FOTOGEN_API_URL", default="")
    if not api:
        raise _InputError("не задан FOTOGEN_API_URL")
    job_id = _fotogen_submit(api, config("FOTOGEN_API_TOKEN", default=""),
                             {"brand": "", "model": args.scene, "category": "editorial",
                              "chat_id": "0", "request_key": args.request_key})
    return {"job_id": job_id, "reused": False}


def cmd_visual_status(args):
    _, _, queue_db = _paths(args)
    row = _queue_row(queue_db, "SELECT status, output_filename, error_text FROM jobs WHERE id=?", (args.job,))
    if row is None:
        return {"status": "missing", "path": None, "error": None}
    status, output, error = row
    path = None
    if status == "done" and output:
        candidate = Path(config("FOTOGEN_OUTPUT_DIR", default="")) / output
        path = str(candidate) if candidate.is_file() else None
    if status == "done" and path is None:
        status = "missing_file"                   # задание закрыто, а файла нет — заказывать заново
    return {"status": status, "path": path, "error": error}


def _post_url(channel: str, message_id) -> str | None:
    if not message_id:
        return None
    channel = str(channel)
    if channel.startswith("@"):
        return f"https://t.me/{channel[1:]}/{message_id}"
    if channel.startswith("-100"):
        return f"https://t.me/c/{channel[4:]}/{message_id}"
    return None


_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")


def _draft_key(raw: str) -> str:
    if not _KEY.match(raw or ""):
        raise _InputError(f"недопустимый id черновика: {raw!r}")
    return raw


# Подвал поста оркестратора: ссылка на раздел сайта и телефон ставит система, не нейросеть
# (владелец, 2026-10-21: «у нас есть сайт, его нужно использовать», консультация, телефон).
SITE_URL = "https://splithome.ru"
SITE_SECTIONS = {                       # тема поста (classify_category) → раздел каталога splithome.ru
    "heat_pumps": ("/catalog/heat-pumps/", "тепловые насосы"),
    "air_conditioners": ("/catalog/?category=2", "кондиционеры"),
    "ventilation": ("/catalog/?category=40", "вентиляцию"),
    "recuperators": ("/catalog/?category=40", "вентиляцию"),
}
CONTACT_PHONE = "+7 978 579-29-95"      # публичный номер с splithome.ru
AGENT_VK_TIMES = ("15:00",)


def site_link(caption: str, channel: str, key: str) -> tuple[str, str]:
    """(ссылка с UTM, что смотреть) — раздел сайта по теме поста."""
    from urllib.parse import urlencode
    from content_factory.orchestrator.vk_content_plan import classify_category
    path, label = SITE_SECTIONS.get(classify_category(caption), ("/catalog/", "каталог"))
    base = config("AGENT_SITE_URL", default=SITE_URL).rstrip("/")
    utm = urlencode({"utm_source": "vk" if channel == "vk_editorial" else "telegram",
                     "utm_medium": "organic_social", "utm_campaign": "orchestrator", "utm_content": key})
    return f"{base}{path}{'&' if '?' in path else '?'}{utm}", label


def compose_post(text: str, channel: str, key: str) -> str:
    """Готовый текст поста: тело редактора + подвал (раздел сайта, консультация по телефону)."""
    url, label = site_link(text, channel, key)
    phone = config("AGENT_CONTACT_PHONE", default=CONTACT_PHONE)
    lines = ["", "━━━━━━━━━━"]
    if channel == "vk_editorial":                 # в Telegram ссылка — кнопкой под постом (publish-tg)
        lines.append(f"🔎 Смотреть {label} на сайте: {url}")
    lines.append(f"📞 Консультация и подбор: {phone}")
    if channel == "vk_editorial":
        lines.append("💬 Или напишите нам в сообщения группы")
    return text.rstrip() + "\n" + "\n".join(lines)


def cmd_compose(args):
    _draft_key(args.key)
    text = Path(args.caption_file).read_text(encoding="utf-8")
    return {"text": compose_post(text, args.channel, args.key)}


def cmd_publish_tg(args):
    from content_factory.publish.telegram import PublishState, publish_post
    state_db, _, _ = _paths(args)
    _draft_key(args.key)
    caption = Path(args.caption_file).read_text(encoding="utf-8")
    if not Path(args.image).is_file():
        raise _InputError(f"нет картинки: {args.image}")
    channel = config("TELEGRAM_CHANNEL_ID", default="")
    if not channel:
        raise _InputError("не задан TELEGRAM_CHANNEL_ID")
    url, label = site_link(caption, "tg_editorial", args.key)
    markup = json.dumps({"inline_keyboard": [[{"text": f"🔎 {label.capitalize()} на сайте", "url": url}]]},
                        ensure_ascii=False)
    res = publish_post(config("TELEGRAM_BOT_TOKEN"), channel, args.image, caption,
                       key=f"editorial|{args.key}", state=PublishState(state_db), retries=2,
                       reply_markup=markup)
    return {"ok": res.ok, "skipped": res.skipped, "message_id": res.message_id, "error": res.error,
            "url": _post_url(channel, res.message_id)}


def cmd_plan_vk(args):
    """Одобренный владельцем ВК-пост — в штатный ВК-план сразу одобренным, на ближайший
    свободный слот. Отложенную запись создаст cf-vk-plan (его дедупликация и аналитика)."""
    import shutil
    import time as _time
    from content_factory.orchestrator.vk_content_plan import (
        ACTIVE_STATUSES, VkContentPlanStore, VkPlanCandidate, classify_category, plan_slots)
    _draft_key(args.key)
    vk_root = Path(args.vk_root or config("VK_ROOT", default="/opt/content-factory-vk"))
    caption = Path(args.caption_file).read_text(encoding="utf-8")
    image = Path(args.image)
    if not image.is_file():
        raise _InputError(f"нет картинки: {image}")
    assets = vk_root / "assets" / "generated" / "editorial"
    assets.mkdir(parents=True, exist_ok=True)
    card = assets / f"{args.key}{image.suffix or '.png'}"
    shutil.copyfile(image, card)
    store = VkContentPlanStore(Path(config("VK_PLAN_STATE_DB", default=str(vk_root / "state" / "vk-plan.db"))))
    now = datetime.now()
    occupied = {i.due_at for i in store.list() if i.status in ACTIVE_STATUSES}
    soon = int(_time.time()) + 3600                  # не ближе часа: планировщику нужен тик
    # Одобренный владельцем пост не ждёт очереди товаров: у постов оркестратора своё время
    # (15:00, между слотами 11:30/18:30 планировщика). Одобренные пункты планировщик не двигает.
    slot = next((s for s in plan_slots(now, times=AGENT_VK_TIMES) if s not in occupied and s >= soon), None)
    if slot is None:
        return {"ok": False, "error": "в ВК-плане на 14 дней нет свободного времени для постов оркестратора"}
    item_id = store.add(VkPlanCandidate(
        source_key=f"agent:{args.key}", source_ts=_time.time(), caption=caption,
        card_path=str(card.resolve()), category=classify_category(caption), brand="",
        content_type="useful"), slot)
    if item_id is None:
        return {"ok": False, "error": "такой пост уже есть в ВК-плане (дубль)"}
    store.mark_review(item_id, None)
    if not store.approve(item_id):
        return {"ok": False, "error": f"пункт ВК-плана {item_id} не удалось одобрить"}
    return {"ok": True, "plan_id": item_id, "due_at": slot}


# ── Единая лента: что ждёт решения владельца, одобрение пачкой, публикация из очереди ──
FRESH_HOURS = 72                        # превью не старше 3 суток — цены актуальны, можно пачкой
STALE_DAYS = 7                          # старше недели — цены устарели, только снять с ожидания


def _review_url() -> str | None:
    chat = str(config("TELEGRAM_REVIEW_CHANNEL_ID", default=""))
    return f"https://t.me/c/{chat[4:]}/999999999" if chat.startswith("-100") else None


def _vk_store(args):
    from content_factory.orchestrator.vk_content_plan import VkContentPlanStore
    vk_root = Path(getattr(args, "vk_root", None) or config("VK_ROOT", default="/opt/content-factory-vk"))
    return VkContentPlanStore(Path(config("VK_PLAN_STATE_DB", default=str(vk_root / "state" / "vk-plan.db"))))


def cmd_feed(args):
    import time as _time
    from content_factory.orchestrator.confirm_store import ConfirmStore
    state_db, _, _ = _paths(args)
    tg = ConfirmStore(state_db).counts(_time.time(), FRESH_HOURS, STALE_DAYS)
    vk = sum(1 for i in _vk_store(args).list() if i.status == "review" and i.card_path)
    return {"tg": tg, "vk_review": vk, "review_url": _review_url(),
            "fresh_hours": FRESH_HOURS, "stale_days": STALE_DAYS}


def cmd_approve_fresh(args):
    import time as _time
    from content_factory.orchestrator.confirm_store import ConfirmStore
    state_db, _, _ = _paths(args)
    return {"queued": ConfirmStore(state_db).queue_fresh(_time.time(), FRESH_HOURS)}


def cmd_expire_stale(args):
    import time as _time
    from content_factory.orchestrator.confirm_store import ConfirmStore
    state_db, _, _ = _paths(args)
    return {"expired": ConfirmStore(state_db).expire_stale(_time.time(), STALE_DAYS)}


def cmd_vk_approve(args):
    store = _vk_store(args)
    approved = [i.id for i in store.list() if i.status == "review" and store.approve(i.id)]
    return {"approved": len(approved), "ids": approved}


def cmd_publish_next(args):
    """Один пост из очереди — тем же кодом, что кнопка ✅ в канале проверки (подпись, «📩 Заказать»)."""
    from content_factory.bot.run import make_publish_fn
    from content_factory.config import load_config
    from content_factory.orchestrator.confirm_store import ConfirmStore
    from content_factory.publish.orders import OrderLinks
    from content_factory.publish.telegram import PublishState
    state_db, _, _ = _paths(args)
    store = ConfirmStore(state_db)
    item = store.next_queued()
    if item is None:
        return {"published": None, "left": 0}
    cfg = load_config(Path("config/config.yaml"))
    state = PublishState(state_db)
    publish_fn = make_publish_fn(config("TELEGRAM_BOT_TOKEN"), cfg.telegram.parse_mode, state,
                                 order_bot=cfg.telegram.order_bot, links=OrderLinks(state_db))
    res = publish_fn(item)
    if res and res.ok:
        store.mark(item.key, "published")
        state.mark(item.key, res.message_id, channel=item.channel, caption=item.caption)
    else:
        store.mark(item.key, "pending")              # не вышло — обратно на ручное решение, очередь не стоит
    left = store.counts(0, 0, 0)["queued"]
    return {"published": item.key if res and res.ok else None, "error": None if res and res.ok
            else getattr(res, "error", "unknown"), "url": _post_url(item.channel, getattr(res, "message_id", None)),
            "left": left}


def _parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state-db")
    common.add_argument("--prices-dir")
    common.add_argument("--queue-db")
    common.add_argument("--no-wake", action="store_true")
    select = argparse.ArgumentParser(add_help=False)
    select.add_argument("--category", required=True)
    select.add_argument("--count", type=int)
    select.add_argument("--quotas")
    select.add_argument("--source")
    select.add_argument("--btu", help="типоразмер kBTU: 9-12, 9,12 или 12")
    select.add_argument("--min-temp", type=int, help="работает на обогрев при морозе не слабее: 20 = до −20 °С")
    parser = argparse.ArgumentParser(prog="agent_cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hint", parents=[common]).set_defaults(fn=cmd_hint)
    sub.add_parser("resolve", parents=[common, select]).set_defaults(fn=cmd_resolve)
    enqueue = sub.add_parser("enqueue", parents=[common, select])
    enqueue.add_argument("--due")
    enqueue.set_defaults(fn=cmd_enqueue)
    cancel = sub.add_parser("cancel", parents=[common])
    cancel.add_argument("--keys", required=True)
    cancel.set_defaults(fn=cmd_cancel)
    status = sub.add_parser("status", parents=[common])
    status.add_argument("--since", type=float)
    status.set_defaults(fn=cmd_status)
    sub.add_parser("workers", parents=[common]).set_defaults(fn=cmd_workers)
    sub.add_parser("start-workers", parents=[common]).set_defaults(fn=cmd_start_workers)
    visual = sub.add_parser("visual-submit", parents=[common])
    visual.add_argument("--scene", required=True)
    visual.add_argument("--request-key", required=True)
    visual.add_argument("--new", action="store_true", help="заказать заново, даже если задание живо")
    visual.set_defaults(fn=cmd_visual_submit)
    vstatus = sub.add_parser("visual-status", parents=[common])
    vstatus.add_argument("--job", type=int, required=True)
    vstatus.set_defaults(fn=cmd_visual_status)
    for name, fn in (("feed", cmd_feed), ("approve-fresh", cmd_approve_fresh), ("expire-stale", cmd_expire_stale),
                     ("vk-approve", cmd_vk_approve), ("publish-next", cmd_publish_next)):
        cmd = sub.add_parser(name, parents=[common])
        cmd.add_argument("--vk-root")
        cmd.set_defaults(fn=fn)
    compose = sub.add_parser("compose", parents=[common])
    compose.add_argument("--key", required=True)
    compose.add_argument("--channel", required=True, choices=("tg_editorial", "vk_editorial"))
    compose.add_argument("--caption-file", required=True)
    compose.set_defaults(fn=cmd_compose)
    for name, fn in (("publish-tg", cmd_publish_tg), ("plan-vk", cmd_plan_vk)):
        pub = sub.add_parser(name, parents=[common])
        pub.add_argument("--key", required=True)
        pub.add_argument("--caption-file", required=True)
        pub.add_argument("--image", required=True)
        if name == "plan-vk":
            pub.add_argument("--vk-root")
        pub.set_defaults(fn=fn)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = args.fn(args)
    except _InputError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
