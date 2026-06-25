"""Очередь задач (SQLite). Каждая запись — СЛОТ расписания: задача × время публикации.
Задача с расписанием [10:00, 14:00] разворачивается в два слота. Планировщик берёт
«дозревшие» слоты (due_at ≤ now) со статусом pending и исполняет по одному разу.
Пустое расписание = немедленный слот (due_at='')."""
from __future__ import annotations
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from content_factory.orchestrator.tasks import Task


@dataclass
class Slot:
    task_id: str
    due_at: str               # "YYYY-MM-DD HH:MM" или "" (немедленно)
    count: int
    mode: str
    channel: str
    confirm: bool
    filter: dict
    status: str


class TaskQueue:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._c() as c:
            c.execute("CREATE TABLE IF NOT EXISTS slots ("
                      "task_id TEXT, due_at TEXT, count INTEGER, mode TEXT, channel TEXT, "
                      "confirm INTEGER, filter_json TEXT, status TEXT DEFAULT 'pending', "
                      "PRIMARY KEY (task_id, due_at))")

    def _c(self):
        return sqlite3.connect(self.path)

    def add(self, task: Task) -> None:
        """Развернуть задачу в слоты. Повторный add не трогает уже существующие слоты
        (INSERT OR IGNORE) — статус done сохраняется."""
        due_times = task.schedule or [""]            # пустое расписание → один немедленный слот
        fj = json.dumps(task.filter, ensure_ascii=False)
        with self._c() as c:
            for due in due_times:
                c.execute("INSERT OR IGNORE INTO slots"
                          "(task_id, due_at, count, mode, channel, confirm, filter_json, status) "
                          "VALUES (?,?,?,?,?,?,?, 'pending')",
                          (task.id, due, task.count, task.mode, task.channel,
                           int(task.confirm), fj))

    def _row_to_slot(self, r) -> Slot:
        return Slot(task_id=r[0], due_at=r[1], count=r[2], mode=r[3], channel=r[4],
                    confirm=bool(r[5]), filter=json.loads(r[6] or "{}"), status=r[7])

    def due(self, now: str) -> list[Slot]:
        """Дозревшие pending-слоты: due_at пустой (немедленно) или ≤ now (лексикографически
        корректно для формата YYYY-MM-DD HH:MM). По возрастанию времени."""
        with self._c() as c:
            rows = c.execute(
                "SELECT task_id, due_at, count, mode, channel, confirm, filter_json, status "
                "FROM slots WHERE status='pending' AND (due_at='' OR due_at<=?) "
                "ORDER BY due_at", (now,)).fetchall()
        return [self._row_to_slot(r) for r in rows]

    def mark_done(self, task_id: str, due_at: str) -> None:
        self.mark_status(task_id, due_at, "done")

    def mark_status(self, task_id: str, due_at: str, status: str) -> None:
        with self._c() as c:
            c.execute("UPDATE slots SET status=? WHERE task_id=? AND due_at=?",
                      (status, task_id, due_at))

    def cancel(self, task_id: str) -> int:
        """Отменить все ещё не исполненные (pending) слоты задачи. Возвращает их число."""
        with self._c() as c:
            cur = c.execute("UPDATE slots SET status='cancelled' "
                            "WHERE task_id=? AND status='pending'", (task_id,))
            return cur.rowcount

    def all_slots(self) -> list[Slot]:
        with self._c() as c:
            rows = c.execute(
                "SELECT task_id, due_at, count, mode, channel, confirm, filter_json, status "
                "FROM slots ORDER BY due_at").fetchall()
        return [self._row_to_slot(r) for r in rows]
