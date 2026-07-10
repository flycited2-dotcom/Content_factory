"""Состояние пошагового диалога постановки задачи в боте (/task): владелец вместо
запоминания синтаксиса /make проходит шаги — категория (кнопки из прайса или
текст) → выбор позиций из автосписка (или свой список строк) → время выгрузки
(«🚀 сейчас» / «завтра 9:00») → (для «сейчас») опц. фото → опц. УТП →
подтверждение. Чистая логика без Telegram (см. bot/run.py для оркестрации);
состояние в SQLite, переживает рестарт cf-bot."""
from __future__ import annotations
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

STEPS = ("awaiting_category", "awaiting_pick", "awaiting_list", "awaiting_time",
         "awaiting_photo", "awaiting_utp", "awaiting_confirm",
         "awaiting_manual_name", "awaiting_manual_price", "awaiting_markup")


@dataclass
class WizardState:
    chat_id: str
    step: str
    category: str | None
    lines: list[str] | None
    photo_path: str | None
    utp_text: str | None
    # кандидаты автосписка (и после выбора — выбранные позиции):
    # [(key, brand, model, name, price), …]
    candidates: list | None = None
    due_at: float | None = None        # None = «сейчас», иначе unix-время выгрузки


class WizardStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._c() as c:
            c.execute("CREATE TABLE IF NOT EXISTS wizard_state ("
                      "chat_id TEXT PRIMARY KEY, step TEXT, category TEXT, "
                      "lines_json TEXT, photo_path TEXT, utp_text TEXT, "
                      "candidates_json TEXT, due_at REAL)")
            for col, typ in (("candidates_json", "TEXT"), ("due_at", "REAL")):
                try:      # миграция прод-таблицы (SQLite: колонку — только ALTER)
                    c.execute(f"ALTER TABLE wizard_state ADD COLUMN {col} {typ}")
                except sqlite3.OperationalError:
                    pass                           # колонка уже есть

    def _c(self):
        return sqlite3.connect(self.path)

    def start(self, chat_id: str) -> None:
        """Начать (или перезапустить с нуля) диалог для chat_id."""
        with self._c() as c:
            c.execute("INSERT INTO wizard_state(chat_id, step) VALUES(?, ?) "
                      "ON CONFLICT(chat_id) DO UPDATE SET step=excluded.step, "
                      "category=NULL, lines_json=NULL, photo_path=NULL, utp_text=NULL, "
                      "candidates_json=NULL, due_at=NULL",
                      (chat_id, "awaiting_category"))

    def set_candidates(self, chat_id: str, category: str, candidates: list) -> None:
        """Автосписок по категории показан — ждём выбора номеров."""
        with self._c() as c:
            c.execute("UPDATE wizard_state SET category=?, candidates_json=?, step=? "
                      "WHERE chat_id=?",
                      (category, json.dumps(candidates, ensure_ascii=False),
                       "awaiting_pick", chat_id))

    def set_pick(self, chat_id: str, picked: list) -> None:
        """Выбранные позиции автосписка — дальше время выгрузки."""
        with self._c() as c:
            c.execute("UPDATE wizard_state SET candidates_json=?, step=? WHERE chat_id=?",
                      (json.dumps(picked, ensure_ascii=False), "awaiting_time", chat_id))

    def to_markup(self, chat_id: str) -> None:
        """Кнопка «💹 Наценка партии» на подтверждении — ждём ±проценты."""
        with self._c() as c:
            c.execute("UPDATE wizard_state SET step=? WHERE chat_id=?",
                      ("awaiting_markup", chat_id))

    def update_prices(self, chat_id: str, candidates: list) -> None:
        """Пересчитанные цены партии → обратно на подтверждение."""
        with self._c() as c:
            c.execute("UPDATE wizard_state SET candidates_json=?, step=? WHERE chat_id=?",
                      (json.dumps(candidates, ensure_ascii=False),
                       "awaiting_confirm", chat_id))

    def to_manual(self, chat_id: str) -> None:
        """Ветка «свой товар» (не из прайсов/остатков) — ждём название."""
        with self._c() as c:
            c.execute("UPDATE wizard_state SET step=? WHERE chat_id=?",
                      ("awaiting_manual_name", chat_id))

    def set_manual_name(self, chat_id: str, name: str) -> None:
        """Название своего товара (кладём в category — видно в подтверждении)."""
        with self._c() as c:
            c.execute("UPDATE wizard_state SET category=?, step=? WHERE chat_id=?",
                      (name, "awaiting_manual_price", chat_id))

    def set_category(self, chat_id: str, category: str) -> None:
        with self._c() as c:
            c.execute("UPDATE wizard_state SET category=?, step=? WHERE chat_id=?",
                      (category, "awaiting_list", chat_id))

    def set_list(self, chat_id: str, lines: list[str]) -> None:
        """Свой список строк (старый путь match_model_lines) — дальше время."""
        with self._c() as c:
            c.execute("UPDATE wizard_state SET lines_json=?, step=? WHERE chat_id=?",
                      (json.dumps(lines, ensure_ascii=False), "awaiting_time", chat_id))

    def set_time(self, chat_id: str, due_at: float | None) -> None:
        """None = «сейчас» → можно приложить фото/УТП; будущее время → сразу
        подтверждение (фото-override дёргает агент немедленно и сломал бы расписание)."""
        step = "awaiting_photo" if due_at is None else "awaiting_confirm"
        with self._c() as c:
            c.execute("UPDATE wizard_state SET due_at=?, step=? WHERE chat_id=?",
                      (due_at, step, chat_id))

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
            row = c.execute("SELECT step, category, lines_json, photo_path, utp_text, "
                            "candidates_json, due_at FROM wizard_state WHERE chat_id=?",
                            (chat_id,)).fetchone()
        if not row:
            return None
        step, category, lines_json, photo_path, utp_text, cand_json, due_at = row
        return WizardState(
            chat_id=chat_id, step=step, category=category,
            lines=json.loads(lines_json) if lines_json is not None else None,
            photo_path=photo_path, utp_text=utp_text,
            candidates=json.loads(cand_json) if cand_json is not None else None,
            due_at=due_at)
