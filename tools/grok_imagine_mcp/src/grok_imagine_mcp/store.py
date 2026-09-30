"""Задачи на диске: по файлу на задачу, чтобы их видели все процессы сервера (Claude Code и Codex)."""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

_ID_RE = re.compile(r"^[0-9a-f]{12}$")


class TaskStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:12]

    def _path(self, task_id: str) -> Path:
        # task_id приходит от модели: принимаем только свой формат, чтобы ../ не вывел за пределы папки
        if not isinstance(task_id, str) or not _ID_RE.match(task_id):
            raise KeyError(task_id)
        return self.root / f"{task_id}.json"

    def save(self, task: dict) -> None:
        path = self._path(task["task_id"])
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def load(self, task_id: str) -> dict:
        path = self._path(task_id)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise KeyError(task_id) from None

    def list(self, limit: int) -> list[dict]:
        out = []
        for p in self.root.glob("*.json") if self.root.is_dir() else []:
            if not _ID_RE.match(p.stem):
                continue
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        out.sort(key=lambda t: t.get("created_at", ""), reverse=True)
        return out[:limit]
