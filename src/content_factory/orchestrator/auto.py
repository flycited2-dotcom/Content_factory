"""Постоянные авто-задачи (полный автомат). Конфиг auto_tasks (yaml) на каждом тике
планировщика разворачивается в слоты «на сегодня»: task_id = auto-<id>-<дата>, поэтому
каждый день появляются свежие слоты, а повторный тик ничего не дублирует
(TaskQueue.add — INSERT OR IGNORE по (task_id, due_at), done-статусы сохраняются).
confirm по умолчанию ВКЛ — всё идёт через ревью-канал владельца."""
from __future__ import annotations
import json
import re
import sqlite3
from collections import Counter
from datetime import date, datetime
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


def _load_auto_override(db) -> list | None:
    """Расписание автомата, настроенное из бота (/auto times|count|cats)."""
    with _settings_c(db) as c:
        row = c.execute("SELECT value FROM settings "
                        "WHERE key='auto_tasks_override'").fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except ValueError:
        return None


def _save_auto_override(db, tasks: list | None) -> None:
    with _settings_c(db) as c:
        if tasks is None:
            c.execute("DELETE FROM settings WHERE key='auto_tasks_override'")
        else:
            c.execute("INSERT OR REPLACE INTO settings (key, value) "
                      "VALUES ('auto_tasks_override', ?)",
                      (json.dumps(tasks, ensure_ascii=False),))


def effective_auto_tasks(db, yaml_tasks: list) -> list:
    """Действующее расписание: override из бота (одна настраиваемая задача)
    или yaml как есть."""
    return _load_auto_override(db) or yaml_tasks


def maybe_materialize(auto_cfgs: list, today: date, queue, db) -> list[Task]:
    """Материализация с учётом выключателя (/auto). ВЫКЛ → не создавать слоты
    И отменить уже созданные pending auto-* (страховка на каждом тике: даже
    сегодняшние не исполнятся). Ручные задачи (/plan, /task) не трогаются."""
    if not auto_enabled(db):
        queue.cancel_auto()
        return []
    return materialize_auto_tasks(effective_auto_tasks(db, auto_cfgs), today, queue)


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


def _override_id(base: dict) -> str:
    """Стабильный id override-задачи ОТ СОДЕРЖИМОГО: тот же конфиг → тот же id
    (тик идемпотентен), изменение настроек → новый id → слоты пересоздаются
    (INSERT OR IGNORE не воскресил бы отменённые с тем же PK). hash() нельзя —
    нестабилен между процессами."""
    import zlib
    sig = json.dumps([base.get("times"), base.get("count"), base.get("filter")],
                     sort_keys=True, ensure_ascii=False)
    return f"custom{zlib.crc32(sig.encode()) % 10000:04d}"


def _auto_edit(verb: str, val: str, auto_cfgs: list, queue, db) -> str:
    """Редактор расписания (/auto times|count|cats <значение>): override поверх
    yaml — одна настраиваемая задача, недостающее наследуется из yaml."""
    ov = _load_auto_override(db)
    if ov:
        base = dict(ov[0])
    else:                                          # первое редактирование — склейка yaml
        base = {
            "filter": dict((auto_cfgs[0].get("filter") if auto_cfgs else None)
                           or {"categories": [2, 6, 7]}),
            "count": int(auto_cfgs[0].get("count", 2)) if auto_cfgs else 2,
            "times": sorted({t for d in auto_cfgs for t in (d.get("times") or [])})
                     or ["10:00"],
        }
    if verb == "times":
        norm = []
        for t in [x.strip() for x in val.replace(";", ",").split(",") if x.strip()]:
            m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
            if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
                return f"❌ не понял время «{t}» — формат ЧЧ:ММ через запятую"
            norm.append(f"{int(m.group(1)):02d}:{m.group(2)}")
        if not norm:
            return "❌ времена пустые — напр.: /auto times 09:00, 13:00, 18:00"
        base["times"] = norm
        msg = "🕐 времена слотов: " + ", ".join(norm)
    elif verb == "count":
        if not val.strip().isdigit() or int(val.strip()) < 1:
            return "❌ количество — целое число ≥ 1, напр.: /auto count 3"
        base["count"] = int(val.strip())
        msg = f"🔢 серий на слот: {base['count']}"
    else:                                          # cats
        nums = re.findall(r"\d+", val)
        if not nums:
            return "❌ категории — id через запятую, напр.: /auto cats 2,6,7"
        base["filter"] = {"categories": [int(n) for n in nums]}
        msg = "📦 категории: " + ", ".join(nums)
    base["id"] = _override_id(base)
    _save_auto_override(db, [base])
    n = queue.cancel_auto()                        # старое расписание — долой
    return (f"{msg}\nСтарых слотов отменено: {n}; новые создаст планировщик "
            f"(≤5 мин, если автомат включён). Сброс к yaml: /auto reset")


def auto_command(arg: str | None, auto_cfgs: list, queue, db, now: datetime) -> str:
    """Ответ на /auto [on|off|times …|count …|cats …|reset]. Чистая логика
    (now/queue/db инжектятся — бот собирает замыкание). Иначе → статус."""
    head, _, tail = (arg or "").partition(" ")
    if head == "reset":
        _save_auto_override(db, None)
        n = queue.cancel_auto()
        return (f"↩️ расписание автомата — снова из config.yaml "
                f"(старых слотов отменено: {n})")
    if head in ("times", "count", "cats"):
        return _auto_edit(head, tail, auto_cfgs, queue, db)
    if arg == "off":
        set_auto_enabled(db, False)
        n = queue.cancel_auto()
        return f"⏸ Авто-контент выключен. Отменено слотов: {n}.\nВключить: /auto on"
    if arg == "on":
        if not auto_cfgs:
            return "❌ в config.yaml нет auto_tasks — включать нечего"
        set_auto_enabled(db, True)
        n = queue.uncancel_auto(now.strftime("%Y-%m-%d %H:%M"))
        return (f"▶️ Авто-контент включён. Сегодня ещё слотов: {n} "
                f"(новые дни создаст планировщик).\nВыключить: /auto off")

    on = auto_enabled(db)
    cfgs = effective_auto_tasks(db, auto_cfgs)
    custom = _load_auto_override(db) is not None
    lines = ["▶️ Авто-контент: ВКЛЮЧЁН" if on else "⏸ Авто-контент: ВЫКЛЮЧЕН"]
    if cfgs:
        per_day = sum(len(d.get("times") or []) * int(d.get("count") or 0)
                      for d in cfgs)
        src = "настроено из бота, /auto reset — к yaml" if custom else "config.yaml"
        lines.append(f"Расписание ({per_day} серий/день, {src}):")
        for d in cfgs:
            cats = ",".join(str(c) for c in (d.get("filter") or {}).get("categories", []))
            lines.append(f"— {d.get('id')}: {', '.join(d.get('times') or [])} "
                         f"× {d.get('count')}" + (f" · кат. {cats}" if cats else ""))
    else:
        lines.append("В config.yaml нет auto_tasks.")
    today = now.strftime("%Y-%m-%d")
    cnt = Counter(s.status for s in queue.all_slots()
                  if s.task_id.startswith("auto-") and s.due_at.startswith(today))
    if cnt:
        lines.append("Сегодня: " + ", ".join(f"{k} {v}" for k, v in sorted(cnt.items())))
    lines.append("Выключить: /auto off" if on else "Включить: /auto on")
    return "\n".join(lines)
