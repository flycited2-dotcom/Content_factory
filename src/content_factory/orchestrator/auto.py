"""Постоянные авто-задачи (полный автомат). Конфиг auto_tasks (yaml) на каждом тике
планировщика разворачивается в слоты «на сегодня»: task_id = auto-<id>-<дата>, поэтому
каждый день появляются свежие слоты, а повторный тик ничего не дублирует
(TaskQueue.add — INSERT OR IGNORE по (task_id, due_at), done-статусы сохраняются).
confirm по умолчанию ВКЛ — всё идёт через ревью-канал владельца."""
from __future__ import annotations
from datetime import date
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
