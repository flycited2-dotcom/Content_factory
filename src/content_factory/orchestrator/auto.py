"""Постоянные авто-задачи (полный автомат). Конфиг auto_tasks (yaml) на каждом тике
планировщика разворачивается в слоты «на сегодня»: task_id = auto-<id>-<дата>, поэтому
каждый день появляются свежие слоты, а повторный тик ничего не дублирует
(TaskQueue.add — INSERT OR IGNORE по (task_id, due_at), done-статусы сохраняются).
confirm по умолчанию ВКЛ — всё идёт через ревью-канал владельца."""
from __future__ import annotations
import sqlite3
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from content_factory.orchestrator.tasks import Task


def materialize_auto_tasks(auto_cfgs: list, today: date, queue) -> list[Task]:
    tasks = []
    for d in auto_cfgs or []:
        aid = d.get("id")
        if not aid:
            raise ValueError("auto_tasks: у задачи нет 'id'")
        if d.get("count") is None:
            raise ValueError(f"auto_tasks {aid}: не указан 'count' (сколько серий за слот)")
        times = d.get("times")
        if not times or not isinstance(times, list):
            raise ValueError(f"auto_tasks {aid}: нужен список 'times' (['HH:MM', …])")
        t = Task(id=f"auto-{aid}-{today.isoformat()}",
                 filter=d.get("filter", {}) or {},
                 count=int(d["count"]),
                 mode=d.get("mode", "mcp"),
                 schedule=[f"{today.isoformat()} {tm}" for tm in times],
                 channel=d.get("channel", "") or "",
                 confirm=bool(d.get("confirm", True)))
        queue.add(t)
        tasks.append(t)
    return tasks


def maybe_materialize(auto_cfgs: list, today: date, queue, db) -> list[Task]:
    """Материализация с учётом выключателя (/auto). ВЫКЛ → не создавать слоты
    И отменить уже созданные pending auto-* (страховка на каждом тике: даже
    сегодняшние не исполнятся). Ручные задачи (/plan, /task) не трогаются."""
    if not auto_enabled(db):
        queue.cancel_auto()
        return []
    return materialize_auto_tasks(auto_cfgs, today, queue)


def _settings_c(db) -> sqlite3.Connection:
    """Соединение с таблицей settings (key-value) в state-БД. Создаёт при первом
    обращении — как остальные сторы (CREATE TABLE IF NOT EXISTS)."""
    p = Path(db)
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    return c


def auto_enabled(db) -> bool:
    """Флаг автомата. НЕТ ЗАПИСИ = ВЫКЛЮЧЕНО (решение владельца 2026-07-09:
    после деплоя авто-контент молчит, пока явно не включат /auto on)."""
    with _settings_c(db) as c:
        row = c.execute("SELECT value FROM settings WHERE key='auto_enabled'").fetchone()
    return bool(row) and row[0] == "1"


def set_auto_enabled(db, on: bool) -> None:
    with _settings_c(db) as c:
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('auto_enabled', ?)",
                  ("1" if on else "0",))


def auto_command(arg: str | None, auto_cfgs: list, queue, db, now: datetime) -> str:
    """Ответ на /auto [on|off]. Чистая логика (now/queue/db инжектятся —
    бот собирает замыкание). Любой другой аргумент → статус."""
    if arg == "off":
        set_auto_enabled(db, False)
        n = queue.cancel_auto()
        return f"⏸ Авто-контент выключен. Отменено слотов: {n}.\nВключить: /auto on"
    if arg == "on":
        if not auto_cfgs:
            return "❌ в config.yaml нет auto_tasks — включать нечего"
        set_auto_enabled(db, True)
        n = queue.uncancel_auto(now.strftime("%Y-%m-%d %H:%M"))
        return (f"▶️ Авто-контент включён. Сегодня ещё слотов: {n} "
                f"(новые дни создаст планировщик).\nВыключить: /auto off")

    on = auto_enabled(db)
    lines = ["▶️ Авто-контент: ВКЛЮЧЁН" if on else "⏸ Авто-контент: ВЫКЛЮЧЕН"]
    if auto_cfgs:
        per_day = sum(len(d.get("times") or []) * int(d.get("count") or 0)
                      for d in auto_cfgs)
        lines.append(f"Расписание ({per_day} серий/день):")
        for d in auto_cfgs:
            lines.append(f"— {d.get('id')}: {', '.join(d.get('times') or [])} "
                         f"× {d.get('count')}")
    else:
        lines.append("В config.yaml нет auto_tasks.")
    today = now.strftime("%Y-%m-%d")
    cnt = Counter(s.status for s in queue.all_slots()
                  if s.task_id.startswith("auto-") and s.due_at.startswith(today))
    if cnt:
        lines.append("Сегодня: " + ", ".join(f"{k} {v}" for k, v in sorted(cnt.items())))
    lines.append("Выключить: /auto off" if on else "Включить: /auto on")
    return "\n".join(lines)
