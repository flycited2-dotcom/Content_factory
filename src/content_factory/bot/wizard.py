"""Состояние пошагового диалога постановки задачи в боте (/task): владелец вместо
запоминания синтаксиса /make проходит шаги — категория → список моделей → (опц.)
фото → (опц.) УТП → подтверждение. Чистая логика без Telegram (см. bot/run.py для
оркестрации); состояние в SQLite, переживает рестарт cf-bot."""
from __future__ import annotations
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

STEPS = ("awaiting_category", "awaiting_list", "awaiting_photo",
        "awaiting_utp", "awaiting_confirm")


@dataclass
class WizardState:
    chat_id: str
    step: str
    category: str | None
    lines: list[str] | None
    photo_path: str | None
    utp_text: str | None


class WizardStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._c() as c:
            c.execute("CREATE TABLE IF NOT EXISTS wizard_state ("
                      "chat_id TEXT PRIMARY KEY, step TEXT, category TEXT, "
                      "lines_json TEXT, photo_path TEXT, utp_text TEXT)")

    def _c(self):
        return sqlite3.connect(self.path)

    def start(self, chat_id: str) -> None:
        """Начать (или перезапустить с нуля) диалог для chat_id."""
        with self._c() as c:
            c.execute("INSERT INTO wizard_state(chat_id, step) VALUES(?, ?) "
                      "ON CONFLICT(chat_id) DO UPDATE SET step=excluded.step, "
                      "category=NULL, lines_json=NULL, photo_path=NULL, utp_text=NULL",
                      (chat_id, "awaiting_category"))

    def set_category(self, chat_id: str, category: str) -> None:
        with self._c() as c:
            c.execute("UPDATE wizard_state SET category=?, step=? WHERE chat_id=?",
                      (category, "awaiting_list", chat_id))

    def set_list(self, chat_id: str, lines: list[str]) -> None:
        with self._c() as c:
            c.execute("UPDATE wizard_state SET lines_json=?, step=? WHERE chat_id=?",
                      (json.dumps(lines, ensure_ascii=False), "awaiting_photo", chat_id))

    def set_photo(self, chat_id: str, photo_path: str | None) -> None:
        with self._c() as c:
            c.execute("UPDATE wizard_state SET photo_path=?, step=? WHERE chat_id=?",
                      (photo_path, "awaiting_utp", chat_id))

    def set_utp(self, chat_id: str, utp_text: str | None) -> None:
        with self._c() as c:
            c.execute("UPDATE wizard_state SET utp_text=?, step=? WHERE chat_id=?",
                      (utp_text, "awaiting_confirm", chat_id))

    def cancel(self, chat_id: str) -> None:
        with self._c() as c:
            c.execute("DELETE FROM wizard_state WHERE chat_id=?", (chat_id,))

    def snapshot(self, chat_id: str) -> WizardState | None:
        with self._c() as c:
            row = c.execute("SELECT step, category, lines_json, photo_path, utp_text "
                            "FROM wizard_state WHERE chat_id=?", (chat_id,)).fetchone()
        if not row:
            return None
        step, category, lines_json, photo_path, utp_text = row
        lines = json.loads(lines_json) if lines_json is not None else None
        return WizardState(chat_id=chat_id, step=step, category=category,
                           lines=lines, photo_path=photo_path, utp_text=utp_text)
