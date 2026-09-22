"""Owner-only role menu. No tokens, arbitrary commands or publication actions."""
from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass

HOME = "🏠 Главное меню"
SECTIONS = {
    "🎬 Контент-завод": "content",
    "📦 Прайсы поставщиков": "prices",
    "📊 Парсеры и остатки": "parsers",
    "⚙️ Система": "system",
}
# Fixed service allowlist: never construct a unit name from Telegram input.
SOURCES = {
    "📦 Остатки I-T-P": "climat-simf-stock-monitor.service",
    "🏨 Заказчики: гостиницы Крыма": "crimea_parser.service",
    "❄️ Заказчики: климат": "hvac_parser.service",
    "🌿 Ритуальный парсер": "ritual_parser.service",
}
SERVICES = ("cf-bot.service", "cf-scheduler.service", "cf-excel.service",
            "cf-cards.service", "cf-mail.service", *SOURCES.values())
LAYOUTS = {
    "home": [["🎬 Контент-завод", "📦 Прайсы поставщиков"],
             ["📊 Парсеры и остатки", "⚙️ Система"], ["🔎 Перейти к тендерам"]],
    "content": [["✍️ Создать задачу", "📋 Очередь"], ["✅ На согласовании"],
                ["🎛 Генерация", "🗓 Расписание контента"]],
    "prices": [["📥 Загрузить прайс", "🕒 Последние обновления"], ["🔍 Найти товар"]],
    "parsers": [["📚 Выбрать источник"], ["🔄 Обновить источник", "🩺 Состояние источника"]],
    "system": [["🩺 Состояние сервисов", "🗓 Расписание сервисов"], ["🗓 Расписание контента"]],
    "sources": [[label] for label in SOURCES],
}
COMMANDS = {"✍️ Создать задачу": "/task", "📋 Очередь": "/status",
            "✅ На согласовании": "/pending", "🎛 Генерация": "/generation",
            "🗓 Расписание контента": "/auto", "🕒 Последние обновления": "/sources",
            "🔍 Найти товар": "/find"}


@dataclass
class MenuReply:
    text: str = ""
    markup: dict | None = None
    command: str = ""


def keyboard(section: str) -> dict:
    rows = list(LAYOUTS.get(section, LAYOUTS["home"]))
    if section != "home":
        rows += [[HOME]]
    return {"keyboard": rows, "resize_keyboard": True, "is_persistent": True}


class ControlMenu:
    def __init__(self, db, owner: str, run=None):
        self.db, self.owner = str(db), str(owner)
        self.run = run or subprocess.run
        with sqlite3.connect(self.db) as c:
            c.execute("CREATE TABLE IF NOT EXISTS bot_menu (chat TEXT PRIMARY KEY, "
                      "section TEXT NOT NULL DEFAULT 'home', source TEXT NOT NULL DEFAULT '')")
            c.execute("CREATE TABLE IF NOT EXISTS bot_menu_start (chat TEXT PRIMARY KEY, source TEXT NOT NULL)")

    def state(self, chat: str) -> tuple[str, str]:
        with sqlite3.connect(self.db) as c:
            row = c.execute("SELECT section, source FROM bot_menu WHERE chat=?", (chat,)).fetchone()
        return tuple(row) if row else ("content", "")

    def _set(self, chat, section, source):
        with sqlite3.connect(self.db) as c:
            c.execute("INSERT INTO bot_menu VALUES(?,?,?) ON CONFLICT(chat) DO UPDATE "
                      "SET section=excluded.section, source=excluded.source", (chat, section, source))

    def _systemctl(self, args) -> str:
        try:
            result = self.run(["systemctl", *args], capture_output=True, text=True, timeout=10)
            if result.returncode:
                return "⚠️ Команда не выполнена: проверьте доступность сервиса и права cf-bot."
            return result.stdout.strip() or "Запрос принят systemd; завершение ещё не подтверждено."
        except (OSError, subprocess.TimeoutExpired):
            return "⚠️ Система управления сервисами недоступна."

    def handle(self, text: str, chat: str, sender: str) -> MenuReply | None:
        # Menu access fails closed, including when owner is not configured.
        if not self.owner or str(chat) != self.owner or str(sender) != self.owner:
            return None
        text = text.strip()
        if text.startswith("/"):
            head, *tail = text.split(maxsplit=1)
            text = head.split("@", 1)[0] + (" " + tail[0] if tail else "")
        section, source = self.state(chat)
        nav = {HOME: "home", "/start": "home", "/menu": "home",
               "/content": "content", "/prices": "prices", "/parsers": "parsers",
               "/system": "system", "📚 Выбрать источник": "sources", **SECTIONS}
        if text in nav or text in SOURCES:
            with sqlite3.connect(self.db) as c:
                c.execute("DELETE FROM bot_menu_start WHERE chat=?", (chat,))
            section = "parsers" if text in SOURCES else nav[text]
            if text in SOURCES:
                source = text
            self._set(chat, section, source)
            title = next((k for k, v in SECTIONS.items() if v == section), "Никита · Главное меню")
            if section == "sources":
                title = "📚 Выберите источник для ручного обновления"
            elif section == "parsers":
                title += "\nИсточник: " + (source or "не выбран")
            return MenuReply(title + "\nВыберите действие. Тендеры — @S1mfer_bot.", keyboard(section))
        if text in COMMANDS:
            if text == "✍️ Создать задачу":
                self._set(chat, "content", source)
            return MenuReply(command=COMMANDS[text])
        if text in ("/collect", "🔎 Перейти к тендерам"):
            return MenuReply("🔎 Сбор тендеров находится в @S1mfer_bot: команда /collect.",
                             {"inline_keyboard": [[{"text": "Открыть тендерного бота",
                                                     "url": "https://t.me/S1mfer_bot"}]]})
        if text == "📥 Загрузить прайс":
            return MenuReply("📦 Пришлите прайс файлом .xlsx. Он добавится источником; "
                             "бот предложит задать наценку. Загрузка сама по себе не публикует посты.",
                             keyboard("prices"))
        if text == "🩺 Состояние сервисов":
            result = ("Сервисы на VPS. inactive у разового сборщика между запусками — нормально.\n\n" +
                      self._systemctl(["show", *SERVICES, "--property=Id,ActiveState,SubState,Result", "--no-pager"]))
        elif text == "🗓 Расписание сервисов":
            timers = [unit.replace(".service", ".timer") for unit in SERVICES if unit != "cf-bot.service"]
            result = self._systemctl(["list-timers", "--all", "--no-pager", *timers])
        elif text in ("🔄 Обновить источник", "🩺 Состояние источника", "▶️ Подтвердить обновление"):
            if source not in SOURCES:
                return MenuReply("Сначала выберите источник.", keyboard("sources"))
            if text == "🔄 Обновить источник":
                with sqlite3.connect(self.db) as c:
                    c.execute("INSERT OR REPLACE INTO bot_menu_start VALUES(?,?)", (chat, source))
                return MenuReply(f"Запустить {source}? Это реальный сбор; источник может отправить отчёт в свой чат.",
                                 {"keyboard": [["▶️ Подтвердить обновление"], ["📊 Парсеры и остатки"], [HOME]],
                                  "resize_keyboard": True})
            unit = SOURCES[source]
            if text == "▶️ Подтвердить обновление":
                with sqlite3.connect(self.db) as c:
                    c.execute("BEGIN IMMEDIATE")
                    requested = c.execute("SELECT source FROM bot_menu_start WHERE chat=?", (chat,)).fetchone()
                    c.execute("DELETE FROM bot_menu_start WHERE chat=?", (chat,))
                if requested != (source,):
                    return MenuReply("Подтверждение устарело или уже использовано. Нажмите «Обновить источник».",
                                     keyboard("parsers"))
            args = (["start", "--no-block", unit] if text == "▶️ Подтвердить обновление" else
                    ["show", unit, "--property=Id,ActiveState,SubState,Result,ExecMainExitTimestamp", "--no-pager"])
            result = source + "\n" + self._systemctl(args)
        else:
            return None
        return MenuReply(result[:3900], keyboard(section))
