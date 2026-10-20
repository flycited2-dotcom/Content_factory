"""JSON-интерфейс постановки задач для оркестратора Agents_MY (этап 2).

Оркестратор (Telegram-бот + Claude) не пишет в базу сам: он вызывает эти
подкоманды, а они используют тот же код, что /make, /find и кнопка «Отменить»
в боте. Вывод — одна строка JSON; ошибка входа — {"error": ...} и код 2.

    python -m content_factory.agent_cli hint
    python -m content_factory.agent_cli resolve --category стабилизаторы [--count N]
        [--quotas '{"ресанта": null}'] [--source manual__resanta]
    python -m content_factory.agent_cli enqueue ...те же... [--due "сегодня 12:00"]
    python -m content_factory.agent_cli cancel --keys k1,k2
    python -m content_factory.agent_cli status [--since UNIX]

Квота {"бренд": null} без "*" — «все позиции только этого бренда».
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from decouple import config

_ALL = 10 ** 9


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
    return (PublishState(state_db).published_keys()
            | ConfirmStore(state_db).blocked_keys()
            | ExcelStore(state_db).selection_blocked_keys(queue_db))


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
        try:
            worker_control.request_run()
        except Exception:
            pass                                   # таймер конвейера подхватит сам
    return {"requested": len(chosen), "accepted": accepted_keys,
            "items": [_item_json(s, i) for s, i in chosen if item_key(i) in set(accepted_keys)],
            "due_at": due_at}


def cmd_cancel(args):
    from content_factory.bot.run import make_cancel_excel_fn
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
    return {"cancelled": len(before & after), "text": "\n".join(t for t in texts if t)}


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
