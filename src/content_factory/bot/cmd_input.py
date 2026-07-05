"""Ввод аргументов команд через ForceReply (задача 2, выбор владельца 2026-07-05).
Раньше тап пустой команды (/find, /make, /pick) из меню Telegram сразу отправлял
её без аргумента — бот ругался «что ищем?». Теперь пустая команда переводит бот
в режим ожидания: приглашение с активным полем ввода (ForceReply), следующий текст
владельца становится аргументом. Чистая логика (без Telegram); стор состояния —
в SQLite, как WizardStore, переживает рестарт cf-bot."""
from __future__ import annotations
import sqlite3
from pathlib import Path

# Команда → (текст приглашения, подсказка-плейсхолдер поля ввода).
ARG_COMMANDS: dict[str, tuple[str, str]] = {
    "/find": ("🔎 Что ищем в прайсе? Напишите фразу.", "напр.: генераторы carver"),
    "/make": ("🧩 Что поставить в работу? Число, категория, квоты.",
              "напр.: 10 холодильники beko=3 stinol=*"),
    "/pick": ("✅ Какие номера из /find взять? Через пробел.", "напр.: 1 3 5"),
}


def bare_arg_command(text: str) -> str | None:
    """«Пустой» вызов команды из ARG_COMMANDS (без аргумента) → канонич. '/find'.
    Учитываем суффикс @botname и регистр. Команда с аргументом → None (идёт как
    раньше). Не команда / другая команда → None."""
    parts = (text or "").strip().split()
    if len(parts) != 1:                       # нет токенов или есть аргумент
        return None
    cmd = parts[0].split("@", 1)[0].lower()   # '/find@Bot' → '/find'
    return cmd if cmd in ARG_COMMANDS else None


def prompt_for(cmd: str) -> tuple[str, str]:
    """(текст приглашения, плейсхолдер) для команды из ARG_COMMANDS."""
    return ARG_COMMANDS[cmd]


def resolve_reply(pending_cmd: str | None, text: str) -> str | None:
    """Отложенная команда + ответ владельца → готовый текст команды | None.
    Пусто/нет отложенной → None. Если ответ сам команда (начинается с /) —
    отложенную игнорируем (владелец передумал), тоже None."""
    if not pending_cmd:
        return None
    t = (text or "").strip()
    if not t or t.startswith("/"):
        return None
    return f"{pending_cmd} {t}"


class PendingCmdStore:
    """Какую команду ждёт бот в этом чате (одноразово). Ключ — chat_id."""
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._c() as c:
            c.execute("CREATE TABLE IF NOT EXISTS pending_cmd "
                      "(chat_id TEXT PRIMARY KEY, cmd TEXT)")

    def _c(self):
        return sqlite3.connect(self.path)

    def set(self, chat_id, cmd: str) -> None:
        with self._c() as c:
            c.execute("INSERT INTO pending_cmd(chat_id, cmd) VALUES(?,?) "
                      "ON CONFLICT(chat_id) DO UPDATE SET cmd=excluded.cmd",
                      (str(chat_id), cmd))

    def take(self, chat_id) -> str | None:
        """Взять и очистить (одноразово): следующий текст обрабатываем один раз."""
        with self._c() as c:
            row = c.execute("SELECT cmd FROM pending_cmd WHERE chat_id=?",
                            (str(chat_id),)).fetchone()
            c.execute("DELETE FROM pending_cmd WHERE chat_id=?", (str(chat_id),))
        return row[0] if row else None

    def clear(self, chat_id) -> None:
        with self._c() as c:
            c.execute("DELETE FROM pending_cmd WHERE chat_id=?", (str(chat_id),))
