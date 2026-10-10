"""Состояние пошагового диалога постановки задачи в боте (/task): владелец вместо
запоминания синтаксиса /make проходит шаги — категория (кнопки из прайса или
текст) → выбор позиций из автосписка (или свой список строк) → время выгрузки
(«🚀 сейчас» / «завтра 9:00») → (для «сейчас») опц. фото → опц. УТП →
подтверждение. Чистая логика без Telegram (см. bot/run.py для оркестрации);
состояние в SQLite, переживает рестарт cf-bot."""
from __future__ import annotations
from content_factory.sqlite_state import state_connection
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

STEPS = ("awaiting_source", "awaiting_count", "awaiting_category", "awaiting_pick", "awaiting_list", "awaiting_time",
         "awaiting_photo", "awaiting_utp", "awaiting_confirm",
         "awaiting_manual_name", "awaiting_manual_price", "awaiting_markup")

# Брошенный диалог протухает: без TTL незавершённый визард висел вечно и бот
# трактовал любой поздний текст как ответ на древний вопрос — «вечный черновик»
# с ForceReply у владельца (2026-07-10, застрявшая цена кондиционера МВО)
WIZARD_TTL_SECONDS = 3 * 3600


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
    page: int = 0
    selected_keys: list[str] | None = None
    source_slot: str | None = None


class WizardStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._c() as c:
            c.execute("CREATE TABLE IF NOT EXISTS wizard_state ("
                      "chat_id TEXT PRIMARY KEY, step TEXT, category TEXT, "
                      "lines_json TEXT, photo_path TEXT, utp_text TEXT, "
                      "candidates_json TEXT, due_at REAL, ts REAL)")
            for col, typ in (("candidates_json", "TEXT"), ("due_at", "REAL"),
                             ("ts", "REAL"), ("page", "INTEGER DEFAULT 0"),
                             ("selected_json", "TEXT"), ("source_slot", "TEXT")):
                try:      # миграция прод-таблицы (SQLite: колонку — только ALTER)
                    c.execute(f"ALTER TABLE wizard_state ADD COLUMN {col} {typ}")
                except sqlite3.OperationalError:
                    pass                           # колонка уже есть

    def _c(self):
        return state_connection(self.path)

    def start(self, chat_id: str, source_slot: str | None = None) -> None:
        """Начать (или перезапустить с нуля) диалог для chat_id."""
        with self._c() as c:
            c.execute("INSERT INTO wizard_state(chat_id, step, ts, source_slot) VALUES(?, ?, ?, ?) "
                      "ON CONFLICT(chat_id) DO UPDATE SET step=excluded.step, "
                      "category=NULL, lines_json=NULL, photo_path=NULL, utp_text=NULL, "
                      "candidates_json=NULL, due_at=NULL, page=0, selected_json=NULL, "
                      "source_slot=excluded.source_slot, ts=excluded.ts",
                      (chat_id, "awaiting_category", time.time(), source_slot))

    def to_source(self, chat_id: str) -> None:
        """New /task starts from suppliers, clearing an earlier draft."""
        self.start(chat_id)
        with self._c() as c:
            c.execute("UPDATE wizard_state SET step=? WHERE chat_id=?",
                      ("awaiting_source", chat_id))

    def set_source(self, chat_id: str, source_slot: str) -> None:
        """Persist the supplier; subsequent category resets keep this scope."""
        self.start(chat_id, source_slot=source_slot)

    def to_count(self, chat_id: str) -> None:
        with self._c() as c:
            c.execute("UPDATE wizard_state SET step=?, ts=? WHERE chat_id=?",
                      ("awaiting_count", time.time(), chat_id))

    def to_pick(self, chat_id: str) -> None:
        with self._c() as c:
            c.execute("UPDATE wizard_state SET step=?, ts=? WHERE chat_id=?",
                      ("awaiting_pick", time.time(), chat_id))

    def set_candidates(self, chat_id: str, category: str, candidates: list) -> None:
        """Автосписок по категории показан — ждём выбора номеров."""
        with self._c() as c:
            c.execute("UPDATE wizard_state SET category=?, candidates_json=?, step=?, "
                      "page=0, selected_json=NULL, "
                      "ts=? WHERE chat_id=?",
                      (category, json.dumps(candidates, ensure_ascii=False),
                       "awaiting_pick", time.time(), chat_id))

    def set_browse(self, chat_id: str, *, page=None, selected_keys=None) -> None:
        """Persist catalogue page and basket without changing the candidate list."""
        with self._c() as c:
            if page is not None:
                c.execute("UPDATE wizard_state SET page=?, ts=? WHERE chat_id=?",
                          (max(0, int(page)), time.time(), chat_id))
            if selected_keys is not None:
                c.execute("UPDATE wizard_state SET selected_json=?, ts=? WHERE chat_id=?",
                          (json.dumps(list(dict.fromkeys(selected_keys))), time.time(), chat_id))

    def set_pick(self, chat_id: str, picked: list) -> None:
        """Выбранные позиции автосписка — дальше время выгрузки."""
        with self._c() as c:
            c.execute("UPDATE wizard_state SET candidates_json=?, step=?, ts=? "
                      "WHERE chat_id=?",
                      (json.dumps(picked, ensure_ascii=False), "awaiting_time",
                       time.time(), chat_id))

    def to_markup(self, chat_id: str) -> None:
        """Кнопка «💹 Наценка партии» на подтверждении — ждём ±проценты."""
        with self._c() as c:
            c.execute("UPDATE wizard_state SET step=?, ts=? WHERE chat_id=?",
                      ("awaiting_markup", time.time(), chat_id))

    def update_prices(self, chat_id: str, candidates: list) -> None:
        """Пересчитанные цены партии → обратно на подтверждение."""
        with self._c() as c:
            c.execute("UPDATE wizard_state SET candidates_json=?, step=?, ts=? "
                      "WHERE chat_id=?",
                      (json.dumps(candidates, ensure_ascii=False),
                       "awaiting_confirm", time.time(), chat_id))

    def to_manual(self, chat_id: str) -> None:
        """Ветка «свой товар» (не из прайсов/остатков) — ждём название."""
        with self._c() as c:
            c.execute("UPDATE wizard_state SET step=?, ts=? WHERE chat_id=?",
                      ("awaiting_manual_name", time.time(), chat_id))

    def set_manual_name(self, chat_id: str, name: str) -> None:
        """Название своего товара (кладём в category — видно в подтверждении)."""
        with self._c() as c:
            c.execute("UPDATE wizard_state SET category=?, step=?, ts=? WHERE chat_id=?",
                      (name, "awaiting_manual_price", time.time(), chat_id))

    def set_category(self, chat_id: str, category: str) -> None:
        with self._c() as c:
            c.execute("UPDATE wizard_state SET category=?, step=?, ts=? WHERE chat_id=?",
                      (category, "awaiting_list", time.time(), chat_id))

    def set_list(self, chat_id: str, lines: list[str]) -> None:
        """Свой список строк (старый путь match_model_lines) — дальше время."""
        with self._c() as c:
            c.execute("UPDATE wizard_state SET lines_json=?, step=?, ts=? WHERE chat_id=?",
                      (json.dumps(lines, ensure_ascii=False), "awaiting_time",
                       time.time(), chat_id))

    def set_time(self, chat_id: str, due_at: float | None) -> None:
        """None = «сейчас» → можно приложить фото/УТП; будущее время → сразу
        подтверждение (фото-override дёргает агент немедленно и сломал бы расписание)."""
        step = "awaiting_photo" if due_at is None else "awaiting_confirm"
        with self._c() as c:
            c.execute("UPDATE wizard_state SET due_at=?, step=?, ts=? WHERE chat_id=?",
                      (due_at, step, time.time(), chat_id))

    def set_photo(self, chat_id: str, photo_path: str | None) -> None:
        with self._c() as c:
            c.execute("UPDATE wizard_state SET photo_path=?, step=?, ts=? WHERE chat_id=?",
                      (photo_path, "awaiting_utp", time.time(), chat_id))

    def set_utp(self, chat_id: str, utp_text: str | None) -> None:
        with self._c() as c:
            c.execute("UPDATE wizard_state SET utp_text=?, step=?, ts=? WHERE chat_id=?",
                      (utp_text, "awaiting_confirm", time.time(), chat_id))

    def cancel(self, chat_id: str) -> None:
        with self._c() as c:
            c.execute("DELETE FROM wizard_state WHERE chat_id=?", (chat_id,))

    def snapshot(self, chat_id: str, now: float | None = None) -> WizardState | None:
        with self._c() as c:
            row = c.execute("SELECT step, category, lines_json, photo_path, utp_text, "
                            "candidates_json, due_at, ts, page, selected_json, source_slot FROM wizard_state "
                            "WHERE chat_id=?", (chat_id,)).fetchone()
        if not row:
            return None
        step, category, lines_json, photo_path, utp_text, cand_json, due_at, ts, page, selected, source_slot = row
        # брошенный диалог (без активности дольше TTL, либо строка до миграции
        # без ts) — чистим: иначе бот вечно ждёт ответ на древний вопрос
        if ts is None or (now if now is not None else time.time()) - ts > WIZARD_TTL_SECONDS:
            self.cancel(chat_id)
            return None
        return WizardState(
            chat_id=chat_id, step=step, category=category,
            lines=json.loads(lines_json) if lines_json is not None else None,
            photo_path=photo_path, utp_text=utp_text,
            candidates=json.loads(cand_json) if cand_json is not None else None,
            due_at=due_at, page=page or 0,
            selected_keys=json.loads(selected) if selected else [], source_slot=source_slot)
