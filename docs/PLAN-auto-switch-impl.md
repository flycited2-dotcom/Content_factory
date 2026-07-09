# Выключатель авто-контента (/auto) — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Команда `/auto` (статус/on/off) выключает-включает auto_tasks из Telegram; дефолт после деплоя — ВЫКЛЮЧЕНО. Спек: [PLAN-auto-switch.md](PLAN-auto-switch.md).

**Architecture:** Флаг `auto_enabled` в таблице `settings` state-БД (нет записи = выкл). Планировщик зовёт обёртку `maybe_materialize`: при выкл не создаёт слоты и отменяет pending `auto-*`. Бот — инжекты `auto_fn`/`auto_state_fn` в `handle_command` (паттерн `excel_fn`); вся логика `/auto` — чистая функция `auto_command` в `orchestrator/auto.py`.

**Tech Stack:** Python 3.11, sqlite3, pytest. Без новых зависимостей.

## Global Constraints

- TDD строго: тест → RED → минимальный код → GREEN → коммит (CLAUDE.md).
- Комментарии на русском, по делу; стиль соседнего кода (см. `auto.py`, `queue.py`).
- Секретов в коде нет (флаг — в state-БД, не в .env/yaml).
- Существующие тесты не ломать: `materialize_auto_tasks` НЕ менять.
- Соглашение: `task_id` авто-слотов начинается с `auto-` (`auto.py:22`).
- `due_at` — строки `"YYYY-MM-DD HH:MM"`, сравнение лексикографическое (как `queue.due`).
- Запуск тестов: `python -m pytest tests/<файл> -q` из корня репо.

---

### Task 1: Флаг auto_enabled в state-БД

**Files:**
- Modify: `src/content_factory/orchestrator/auto.py`
- Test: `tests/test_auto.py`

**Interfaces:**
- Produces: `auto_enabled(db) -> bool`, `set_auto_enabled(db, on: bool) -> None`
  (`db` — путь к sqlite-файлу, str | Path). Таблица `settings(key TEXT PRIMARY KEY, value TEXT)`.

- [ ] **Step 1: Write the failing tests** — добавить в конец `tests/test_auto.py`:

```python
def test_auto_enabled_default_off(tmp_path):
    # нет записи = ВЫКЛЮЧЕНО (решение владельца 2026-07-09: после деплоя автомат молчит)
    assert auto_enabled(tmp_path / "s.db") is False


def test_set_auto_enabled_roundtrip(tmp_path):
    db = tmp_path / "s.db"
    set_auto_enabled(db, True)
    assert auto_enabled(db) is True
    set_auto_enabled(db, False)
    assert auto_enabled(db) is False
```

и расширить импорт в шапке файла:

```python
from content_factory.orchestrator.auto import (
    auto_enabled, materialize_auto_tasks, set_auto_enabled)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_auto.py -q`
Expected: ERROR при сборке — `ImportError: cannot import name 'auto_enabled'`

- [ ] **Step 3: Write minimal implementation** — в `src/content_factory/orchestrator/auto.py`.

Заменить импорты в шапке (добавляются sqlite3/Path):

```python
from __future__ import annotations
import sqlite3
from datetime import date
from pathlib import Path
from content_factory.orchestrator.tasks import Task
```

Добавить в конец файла:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_auto.py -q`
Expected: все PASS

- [ ] **Step 5: Commit**

```bash
git add src/content_factory/orchestrator/auto.py tests/test_auto.py
git commit -m "feat(auto): флаг auto_enabled в state-БД — нет записи = ВЫКЛЮЧЕНО"
```

---

### Task 2: TaskQueue.cancel_auto / uncancel_auto

**Files:**
- Modify: `src/content_factory/orchestrator/queue.py` (после метода `cancel`, строка ~78)
- Test: `tests/test_queue.py`

**Interfaces:**
- Produces: `TaskQueue.cancel_auto() -> int` (pending `auto-%` → cancelled),
  `TaskQueue.uncancel_auto(after: str) -> int` (cancelled `auto-%` c `due_at > after` → pending).

- [ ] **Step 1: Write the failing tests** — добавить в конец `tests/test_queue.py`
  (хелпер `_task(**kw)` уже есть в шапке файла):

```python
def test_cancel_auto_only_touches_auto_pending(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    q.add(_task(id="auto-am-2026-07-09"))
    q.add(_task(id="manual-1"))
    q.mark_done("auto-am-2026-07-09", "2026-06-26 10:00")   # done не трогаем
    n = q.cancel_auto()
    assert n == 1                                           # только pending авто-слот
    st = {(s.task_id, s.due_at): s.status for s in q.all_slots()}
    assert st[("auto-am-2026-07-09", "2026-06-26 10:00")] == "done"
    assert st[("auto-am-2026-07-09", "2026-06-26 14:00")] == "cancelled"
    assert st[("manual-1", "2026-06-26 10:00")] == "pending"


def test_uncancel_auto_only_future_auto(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    q.add(_task(id="auto-am-2026-06-26"))
    q.add(_task(id="manual-1"))
    q.cancel("manual-1")
    q.cancel_auto()
    n = q.uncancel_auto("2026-06-26 12:00")                 # 10:00 уже прошло
    assert n == 1
    st = {(s.task_id, s.due_at): s.status for s in q.all_slots()}
    assert st[("auto-am-2026-06-26", "2026-06-26 10:00")] == "cancelled"  # прошлое не воскресло
    assert st[("auto-am-2026-06-26", "2026-06-26 14:00")] == "pending"
    assert st[("manual-1", "2026-06-26 10:00")] == "cancelled"            # ручной не трогаем
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_queue.py -q`
Expected: FAIL — `AttributeError: 'TaskQueue' object has no attribute 'cancel_auto'`

- [ ] **Step 3: Write minimal implementation** — в `queue.py` после метода `cancel`:

```python
    def cancel_auto(self) -> int:
        """Отменить все pending АВТО-слоты (task_id с префиксом auto- — соглашение
        materialize_auto_tasks). Ручные задачи не трогаются. Возвращает их число."""
        with self._c() as c:
            cur = c.execute("UPDATE slots SET status='cancelled' "
                            "WHERE task_id LIKE 'auto-%' AND status='pending'")
            return cur.rowcount

    def uncancel_auto(self, after: str) -> int:
        """Вернуть отменённые авто-слоты с БУДУЩИМ временем в pending (после /auto on
        прошедшие слоты не должны исполниться «догоном» залпом). after — "YYYY-MM-DD HH:MM",
        сравнение лексикографическое (как в due)."""
        with self._c() as c:
            cur = c.execute("UPDATE slots SET status='pending' "
                            "WHERE task_id LIKE 'auto-%' AND status='cancelled' AND due_at>?",
                            (after,))
            return cur.rowcount
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_queue.py -q`
Expected: все PASS

- [ ] **Step 5: Commit**

```bash
git add src/content_factory/orchestrator/queue.py tests/test_queue.py
git commit -m "feat(queue): cancel_auto/uncancel_auto — пауза и возврат авто-слотов"
```

---

### Task 3: maybe_materialize + подключение в планировщик

**Files:**
- Modify: `src/content_factory/orchestrator/auto.py`
- Modify: `src/content_factory/orchestrator/scheduler_run.py:23` (импорт) и `:93` (вызов)
- Test: `tests/test_auto.py`

**Interfaces:**
- Consumes: `auto_enabled(db)` (Task 1), `TaskQueue.cancel_auto()` (Task 2),
  `materialize_auto_tasks(auto_cfgs, today, queue)` (существующая).
- Produces: `maybe_materialize(auto_cfgs, today, queue, db) -> list[Task]`.

- [ ] **Step 1: Write the failing tests** — добавить в `tests/test_auto.py`:

```python
def test_maybe_materialize_off_cancels_auto_keeps_manual(tmp_path):
    # флаг не выставлен = ВЫКЛ: не материализуем И отменяем уже созданные авто-слоты
    q = TaskQueue(tmp_path / "q.db")
    materialize_auto_tasks(CFG, date(2026, 7, 2), q)         # авто-слоты уже в очереди
    q.add(Task(id="manual-1", filter={}, count=1, mode="mcp",
               schedule=["2026-07-02 12:00"], channel="", confirm=True))
    tasks = maybe_materialize(CFG, date(2026, 7, 2), q, tmp_path / "s.db")
    assert tasks == []
    st = {s.task_id: s.status for s in q.all_slots()}
    assert st["manual-1"] == "pending"                       # ручные неприкосновенны
    assert st["auto-ac-2026-07-02"] == "cancelled"


def test_maybe_materialize_on_materializes(tmp_path):
    db = tmp_path / "s.db"
    set_auto_enabled(db, True)
    q = TaskQueue(tmp_path / "q.db")
    tasks = maybe_materialize(CFG, date(2026, 7, 2), q, db)
    assert [t.id for t in tasks] == ["auto-ac-2026-07-02"]
    assert len(q.all_slots()) == 2
```

и расширить импорты шапки `tests/test_auto.py`:

```python
from content_factory.orchestrator.auto import (
    auto_enabled, materialize_auto_tasks, maybe_materialize, set_auto_enabled)
from content_factory.orchestrator.tasks import Task
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_auto.py -q`
Expected: ERROR — `ImportError: cannot import name 'maybe_materialize'`

- [ ] **Step 3: Write minimal implementation** — в `auto.py` после `materialize_auto_tasks`:

```python
def maybe_materialize(auto_cfgs: list, today: date, queue, db) -> list[Task]:
    """Материализация с учётом выключателя (/auto). ВЫКЛ → не создавать слоты
    И отменить уже созданные pending auto-* (страховка на каждом тике: даже
    сегодняшние не исполнятся). Ручные задачи (/plan, /task) не трогаются."""
    if not auto_enabled(db):
        queue.cancel_auto()
        return []
    return materialize_auto_tasks(auto_cfgs, today, queue)
```

- [ ] **Step 4: Подключить в scheduler_run.py** — строка 23, заменить импорт:

```python
from content_factory.orchestrator.auto import maybe_materialize
```

строка 93, заменить вызов:

```python
    maybe_materialize(cfg.auto_tasks, date.today(), q, cfg.state.db)   # автомат с выключателем /auto
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_auto.py tests/test_scheduler.py -q`
Expected: все PASS

- [ ] **Step 6: Commit**

```bash
git add src/content_factory/orchestrator/auto.py src/content_factory/orchestrator/scheduler_run.py tests/test_auto.py
git commit -m "feat(auto): maybe_materialize — планировщик уважает выключатель /auto"
```

---

### Task 4: auto_command — логика ответов /auto

**Files:**
- Modify: `src/content_factory/orchestrator/auto.py`
- Test: `tests/test_auto.py`

**Interfaces:**
- Consumes: `auto_enabled`/`set_auto_enabled` (Task 1), `queue.cancel_auto()/uncancel_auto(after)`
  (Task 2), `queue.all_slots()` (существующий).
- Produces: `auto_command(arg: str | None, auto_cfgs: list, queue, db, now: datetime) -> str` —
  весь текст ответа на `/auto [on|off]`; бот прокидывает замыкание (Task 6).

- [ ] **Step 1: Write the failing tests** — добавить в `tests/test_auto.py`:

```python
def _q_with_auto(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    materialize_auto_tasks(CFG, date(2026, 7, 2), q)
    return q


def test_auto_command_status_off_by_default(tmp_path):
    q = _q_with_auto(tmp_path)
    txt = auto_command(None, CFG, q, tmp_path / "s.db", datetime(2026, 7, 2, 9, 0))
    assert "ВЫКЛЮЧЕН" in txt and "/auto on" in txt
    assert "ac: 10:00, 14:00 × 2" in txt                    # расписание из конфига
    assert "pending 2" in txt                               # слоты сегодня


def test_auto_command_off_flags_and_cancels(tmp_path):
    q = _q_with_auto(tmp_path)
    db = tmp_path / "s.db"
    set_auto_enabled(db, True)
    txt = auto_command("off", CFG, q, db, datetime(2026, 7, 2, 9, 0))
    assert "выключен" in txt and "Отменено слотов: 2" in txt
    assert auto_enabled(db) is False
    assert all(s.status == "cancelled" for s in q.all_slots())


def test_auto_command_on_resurrects_only_future(tmp_path):
    q = _q_with_auto(tmp_path)
    db = tmp_path / "s.db"
    auto_command("off", CFG, q, db, datetime(2026, 7, 2, 9, 0))
    txt = auto_command("on", CFG, q, db, datetime(2026, 7, 2, 12, 0))   # 10:00 уже прошло
    assert "включён" in txt and "Сегодня ещё слотов: 1" in txt
    assert auto_enabled(db) is True
    st = {s.due_at: s.status for s in q.all_slots()}
    assert st == {"2026-07-02 10:00": "cancelled", "2026-07-02 14:00": "pending"}


def test_auto_command_on_without_config(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    txt = auto_command("on", [], q, tmp_path / "s.db", datetime(2026, 7, 2, 9, 0))
    assert "❌" in txt                                       # включать нечего
```

и дополнить импорт datetime в шапке `tests/test_auto.py`:

```python
from datetime import date, datetime
```

плюс добавить `auto_command` в импорт из `content_factory.orchestrator.auto`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_auto.py -q`
Expected: ERROR — `ImportError: cannot import name 'auto_command'`

- [ ] **Step 3: Write minimal implementation** — в `auto.py`.

Дополнить импорты шапки:

```python
from collections import Counter
from datetime import date, datetime
```

Добавить в конец файла:

```python
def auto_command(arg: str | None, auto_cfgs: list, queue, db, now: datetime) -> str:
    """Ответ на /auto [on|off]. Чистая логика (now/queue/db инжектятся —
    бот собирает замыкание). Любой другой аргумент → статус."""
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
    lines = ["▶️ Авто-контент: ВКЛЮЧЁН" if on else "⏸ Авто-контент: ВЫКЛЮЧЕН"]
    if auto_cfgs:
        per_day = sum(len(d.get("times") or []) * int(d.get("count") or 0)
                      for d in auto_cfgs)
        lines.append(f"Расписание ({per_day} серий/день):")
        for d in auto_cfgs:
            lines.append(f"— {d.get('id')}: {', '.join(d.get('times') or [])} "
                         f"× {d.get('count')}")
    else:
        lines.append("В config.yaml нет auto_tasks.")
    today = now.strftime("%Y-%m-%d")
    cnt = Counter(s.status for s in queue.all_slots()
                  if s.task_id.startswith("auto-") and s.due_at.startswith(today))
    if cnt:
        lines.append("Сегодня: " + ", ".join(f"{k} {v}" for k, v in sorted(cnt.items())))
    lines.append("Выключить: /auto off" if on else "Включить: /auto on")
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_auto.py -q`
Expected: все PASS

- [ ] **Step 5: Commit**

```bash
git add src/content_factory/orchestrator/auto.py tests/test_auto.py
git commit -m "feat(auto): auto_command — статус/пауза/включение автомата текстом"
```

---

### Task 5: Роутинг /auto + строка автомата в /status

**Files:**
- Modify: `src/content_factory/bot/commands.py` (HELP ~23, `_status` ~104, `handle_command` ~188, роутинг ~310)
- Test: `tests/test_commands.py`

**Interfaces:**
- Produces: `handle_command(..., auto_fn=None, auto_state_fn=None)`;
  `auto_fn(arg: str | None) -> str` — весь ответ /auto; `auto_state_fn() -> bool | None`
  — None означает «авто не настроено», строка в /status не показывается.

- [ ] **Step 1: Write the failing tests** — добавить в конец `tests/test_commands.py`:

```python
def test_auto_routed_to_auto_fn(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    calls = []
    out = handle_command("/auto off", q, auto_fn=lambda a: calls.append(a) or "OK")
    assert out == "OK" and calls == ["off"]
    out = handle_command("/auto", q, auto_fn=lambda a: f"arg={a}")
    assert out == "arg=None"


def test_auto_unavailable_without_fn(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    assert "недоступен" in handle_command("/auto", q)


def test_status_shows_auto_line(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    out = handle_command("/status", q, auto_state_fn=lambda: False)
    assert "🤖" in out and "/auto on" in out                 # выключен → как включить
    out = handle_command("/status", q, auto_state_fn=lambda: True)
    assert "включён" in out and "/auto off" in out
    out = handle_command("/status", q)                       # авто не настроено — строки нет
    assert "🤖" not in out
```

(если в шапке test_commands.py нет `TaskQueue` — добавить
`from content_factory.orchestrator.queue import TaskQueue`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_commands.py -q`
Expected: FAIL — `handle_command() got an unexpected keyword argument 'auto_fn'`

- [ ] **Step 3: Write minimal implementation** — в `commands.py`.

3a. Сигнатура `handle_command` (строка ~188) — добавить два инжекта в конец:

```python
def handle_command(text: str, queue, today: date | None = None, held_provider=None,
                   confirm_store=None, publish_fn=None, publish_state=None,
                   regen_fn=None, make_fn=None, find_fn=None, pick_fn=None,
                   excel_fn=None, price_fn=None, sources_fn=None, markup_fn=None,
                   auto_fn=None, auto_state_fn=None) -> str:
```

3b. Роутинг — вставить ПЕРЕД `if cmd.startswith("/status")` (важно: не после,
и не внутри других веток):

```python
    if cmd.startswith("/auto"):
        # выключатель авто-контента: /auto [on|off]; логика — orchestrator/auto.py
        if not auto_fn:
            return "❌ авто-контент недоступен"
        return auto_fn(parts[1].lower() if len(parts) > 1 else None)
```

3c. `_status` (строка ~104) — сигнатура и строка автомата в конце:

```python
def _status(queue, auto_state_fn=None) -> str:
```

и заменить последнюю строку функции `return "\n".join(lines) if lines else "Очередь пуста."` на:

```python
    body = "\n".join(lines) if lines else "Очередь пуста."
    auto_on = auto_state_fn() if auto_state_fn else None
    if auto_on is not None:                       # None = авто не настроено — молчим
        body += ("\n🤖 Авто-контент: ▶️ включён (/auto off)" if auto_on
                 else "\n🤖 Авто-контент: ⏸ выключен (/auto on)")
    return body
```

3d. Вызов в роутинге `/status`:

```python
    if cmd.startswith("/status"):
        return _status(queue, auto_state_fn)
```

3e. HELP (строка ~23) — добавить после строки `/status`:

```python
        "/auto — авто-контент: статус, /auto on|off — включить/выключить\n"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_commands.py -q`
Expected: все PASS

- [ ] **Step 5: Commit**

```bash
git add src/content_factory/bot/commands.py tests/test_commands.py
git commit -m "feat(bot): /auto — роутинг и строка автомата в /status"
```

---

### Task 6: Сборка в run.py + финальный прогон

**Files:**
- Modify: `src/content_factory/bot/run.py` (импорты ~7-25, `main()` ~474, вызов handle_command ~731)

**Interfaces:**
- Consumes: `auto_command`, `auto_enabled` (Task 4/1), `handle_command(..., auto_fn, auto_state_fn)` (Task 5).

- [ ] **Step 1: Импорты** — в шапку run.py добавить:

```python
from datetime import datetime
from content_factory.orchestrator.auto import auto_command, auto_enabled
```

(`datetime` в run.py сейчас НЕ импортирован — добавить строку после `import time`.)

- [ ] **Step 2: Замыкания в main()** — после строки `markup_fn = make_markup_fn(prices_dir)` (~474):

```python
    # /auto: выключатель автомата (флаг в state-БД, слоты в общей очереди q)
    def auto_fn(arg):
        return auto_command(arg, cfg.auto_tasks, q, cfg.state.db, datetime.now())

    def auto_state_fn():
        return auto_enabled(cfg.state.db) if cfg.auto_tasks else None
```

- [ ] **Step 3: Прокинуть в handle_command** (~731) — добавить в вызов:

```python
            reply = handle_command(text, q, confirm_store=cs, publish_fn=publish_fn,
                                   publish_state=ps, regen_fn=regen_fn, make_fn=make_fn,
                                   find_fn=find_fn, pick_fn=pick_fn, excel_fn=excel_fn,
                                   price_fn=price_fn, sources_fn=sources_fn,
                                   markup_fn=markup_fn, auto_fn=auto_fn,
                                   auto_state_fn=auto_state_fn)
```

- [ ] **Step 4: Полный прогон**

Run: `python -m pytest -q`
Expected: все PASS (403 было до фичи + новые)

- [ ] **Step 5: Commit**

```bash
git add src/content_factory/bot/run.py
git commit -m "feat(bot): подключить /auto и строку автомата в /status (run.py)"
```

---

## Вне плана (вручную, по явному ОК владельца)

Деплой на VPS 213.109.202.45: tar+ssh (scp не работает), рестарт `cf-bot`
(long-poll; `cf-scheduler` — таймер, новый код подхватит сам). Проверка на проде:
`/auto` → «⏸ ВЫКЛЮЧЕН», `/status` — строка автомата, к следующему тику
pending `auto-*` отменены. Это внешнее действие — НЕ выполнять без подтверждения.
