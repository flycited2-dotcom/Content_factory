"""Вход задач (вариант A) — команды Telegram-боту. Парсер `/plan …` → Task и роутинг
`/status` `/cancel` `/held`. Сам сетевой бот (long-polling/Bot API) — тонкая обёртка
вокруг этих чистых функций (подключается в M6); здесь — вся логика, покрытая тестами.

Пример: /plan 10 кондиционеры завтра 10:00,14:00 mode=mcp [confirm] [source=breeze] [cat=2,6]
"""
from __future__ import annotations
import re
from collections import Counter
from datetime import date, timedelta
from content_factory.orchestrator.tasks import Task

# Ключевые слова категорий → частичный фильтр (расширяемо; для пилота — кондиционеры).
DEFAULT_KEYWORDS = {
    "кондиционеры": {"categories": [2, 6, 7]},
    "кондиционер": {"categories": [2, 6, 7]},
    "настенные": {"categories": [2]},
}

_TIME = re.compile(r"^\d{1,2}:\d{2}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

HELP = ("Команды:\n"
        "/plan <N> <категория> <завтра|сегодня|ДАТА> <ЧЧ:ММ[,ЧЧ:ММ]> "
        "[mode=] [source=] [cat=] [confirm] [channel=] [id=]\n"
        "/status — что в очереди   /cancel <id> — отменить\n"
        "/pending — посты на подтверждении   /approve <key> — опубликовать   "
        "/reject <key> — отклонить   /held — отложенные")


def _norm_time(t: str) -> str:
    h, m = t.split(":")
    return f"{int(h):02d}:{m}"


def parse_plan(text: str, today: date | None = None, keyword_filters: dict | None = None) -> Task:
    """Разобрать команду /plan в Task. `today` — опорная дата для 'сегодня/завтра'
    (детерминизм в тестах). Невалидный ввод → ValueError с понятным текстом."""
    today = today or date.today()
    kw = keyword_filters or DEFAULT_KEYWORDS
    parts = (text or "").strip().split()
    if parts and parts[0].lower().startswith("/plan"):
        parts = parts[1:]

    count = None
    mode, channel, tid, confirm = "mcp", "", None, False
    filt: dict = {}
    base_date = today
    times: list[str] = []

    for tok in parts:
        low = tok.lower()
        if count is None and tok.isdigit():
            count = int(tok)
        elif low == "завтра":
            base_date = today + timedelta(days=1)
        elif low == "сегодня":
            base_date = today
        elif _DATE.match(tok):
            y, m, d = map(int, tok.split("-"))
            base_date = date(y, m, d)
        elif "," in tok and tok.split(",")[0] and all(_TIME.match(p) for p in tok.split(",") if p):
            times += [_norm_time(p) for p in tok.split(",") if p]
        elif _TIME.match(tok):
            times.append(_norm_time(tok))
        elif low.startswith("mode="):
            mode = tok.split("=", 1)[1]
        elif low.startswith("source="):
            filt["source"] = tok.split("=", 1)[1]
        elif low.startswith("cat="):
            filt["categories"] = [int(x) for x in tok.split("=", 1)[1].split(",") if x]
        elif low.startswith("channel="):
            channel = tok.split("=", 1)[1]
        elif low.startswith("id="):
            tid = tok.split("=", 1)[1]
        elif low == "confirm":
            confirm = True
        elif low in kw:
            for k, v in kw[low].items():
                filt.setdefault(k, v)
        # неизвестные токены игнорируем (мягкий разбор)

    if count is None:
        raise ValueError("укажите количество серий (число), напр.: /plan 10 кондиционеры завтра 10:00")
    if not times:
        raise ValueError("укажите время (ЧЧ:ММ), напр.: завтра 10:00,14:00")
    if not filt.get("categories") and not filt.get("source"):
        raise ValueError("укажите категорию (напр. 'кондиционеры' или cat=2)")

    schedule = [f"{base_date.isoformat()} {t}" for t in times]
    if not tid:
        tid = f"plan-{base_date.isoformat()}-{times[0].replace(':', '')}-{count}"
    return Task(id=tid, filter=filt, count=count, mode=mode, schedule=schedule,
                channel=channel, confirm=confirm)


def _status(queue) -> str:
    slots = queue.all_slots()
    if not slots:
        return "Очередь пуста."
    by_task: dict[str, Counter] = {}
    for s in slots:
        by_task.setdefault(s.task_id, Counter())[s.status] += 1
    lines = ["Задачи в очереди:"]
    for tid, c in by_task.items():
        lines.append(f"— {tid}: " + ", ".join(f"{k} {v}" for k, v in c.items()))
    return "\n".join(lines)


def handle_command(text: str, queue, today: date | None = None, held_provider=None,
                   confirm_store=None, publish_fn=None, publish_state=None) -> str:
    """Маршрутизация команды → действие → текст ответа владельцу.
    confirm_store/publish_fn/publish_state нужны для confirm-пилота (/approve, /reject, /pending).
    publish_fn(awaiting) -> PublishResult публикует подтверждённый пост в канал."""
    text = (text or "").strip()
    parts = text.split()
    cmd = (parts[0].lower() if parts else "")

    if cmd.startswith("/approve"):
        if not (confirm_store and publish_fn):
            return "❌ подтверждение недоступно"
        if len(parts) < 2:
            return "❌ укажите ключ: /approve <key>"
        key = parts[1]
        a = confirm_store.get(key)
        if not a or a.status != "pending":
            return f"❌ нет поста на подтверждении: {key}"
        res = publish_fn(a)
        if res and res.ok:
            confirm_store.mark(key, "published")
            if publish_state:
                publish_state.mark(key, res.message_id)
            return f"✅ опубликовано: {key}"
        return f"❌ не удалось опубликовать {key}: {getattr(res, 'error', None)}"
    if cmd.startswith("/reject"):
        if not confirm_store or len(parts) < 2:
            return "❌ укажите ключ: /reject <key>"
        confirm_store.mark(parts[1], "rejected")
        return f"Отклонено: {parts[1]}"
    if cmd.startswith("/pending"):
        items = confirm_store.list_pending() if confirm_store else []
        if not items:
            return "На подтверждении ничего нет."
        return "На подтверждении:\n" + "\n".join(f"— {a.key} → /approve {a.key}" for a in items)

    if cmd.startswith("/plan"):
        try:
            t = parse_plan(text, today=today)
        except ValueError as e:
            return f"❌ {e}"
        queue.add(t)
        extra = ", подтверждение ВКЛ" if t.confirm else ""
        return (f"✅ Задача {t.id}: {t.count} серий/слот, слотов {len(t.schedule)}, "
                f"режим {t.mode}{extra}")
    if cmd.startswith("/status"):
        return _status(queue)
    if cmd.startswith("/cancel"):
        parts = text.split()
        if len(parts) < 2:
            return "❌ укажите id задачи: /cancel <id>"
        n = queue.cancel(parts[1])
        return f"Задача {parts[1]}: отменено слотов {n}"
    if cmd.startswith("/held"):
        items = held_provider() if held_provider else []
        if not items:
            return "Отложенных (held) нет."
        return "Отложенные (held):\n" + "\n".join(
            f"— {k}: {', '.join(r)}" for k, r in items)
    return HELP
