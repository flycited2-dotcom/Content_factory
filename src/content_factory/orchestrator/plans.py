"""Вход задач (вариант B) — YAML-планы. Читает tasks/*.yaml → Task в очередь.
Формат — см. examples/tasks.example.yaml. Расписание задаётся СПИСКОМ локальных времён
('YYYY-MM-DD HH:MM'). Cron-форма пока не поддержана (понятная ошибка)."""
from __future__ import annotations
from pathlib import Path
import yaml
from content_factory.orchestrator.tasks import Task


def _to_task(d: dict) -> Task:
    tid = d.get("id")
    if not tid:
        raise ValueError("план: у задачи нет 'id'")
    if d.get("count") is None:
        raise ValueError(f"{tid}: не указан 'count' (сколько серий за слот)")
    sched = d.get("schedule", [])
    if isinstance(sched, dict):
        # {cron: ...} — пока не реализовано; для пилота используем список времён
        raise ValueError(f"{tid}: cron-расписание пока не поддержано, задайте список времён "
                         "['YYYY-MM-DD HH:MM', ...]")
    if sched and not isinstance(sched, list):
        raise ValueError(f"{tid}: 'schedule' должно быть списком времён")
    return Task(id=str(tid), filter=d.get("filter", {}) or {}, count=int(d["count"]),
                mode=d.get("mode", "mcp"), schedule=[str(s) for s in (sched or [])],
                channel=d.get("channel", "") or "", confirm=bool(d.get("confirm", False)))


def load_plans(path) -> list[Task]:
    """Загрузить задачи из файла .yaml или из директории (все *.yaml/*.yml в ней)."""
    p = Path(path)
    files = sorted([*p.glob("*.yaml"), *p.glob("*.yml")]) if p.is_dir() else [p]
    tasks: list[Task] = []
    for f in files:
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        for d in data.get("tasks", []) or []:
            tasks.append(_to_task(d))
    return tasks


def load_plans_into_queue(path, queue) -> int:
    """Загрузить план(ы) и положить задачи в очередь. Возвращает число задач."""
    tasks = load_plans(path)
    for t in tasks:
        queue.add(t)
    return len(tasks)
