"""Постоянные авто-задачи (полный автомат). Конфиг auto_tasks (yaml) на каждом тике
планировщика разворачивается в слоты «на сегодня»: task_id = auto-<id>-<дата>, поэтому
каждый день появляются свежие слоты, а повторный тик ничего не дублирует
(TaskQueue.add — INSERT OR IGNORE по (task_id, due_at), done-статусы сохраняются).
confirm по умолчанию ВКЛ — всё идёт через ревью-канал владельца."""
from __future__ import annotations
import sqlite3
from datetime import date
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
