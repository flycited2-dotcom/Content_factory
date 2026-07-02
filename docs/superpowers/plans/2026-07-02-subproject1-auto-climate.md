# Подпроект 1 «Автомат климатики» — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Полный автомат для климатики: постоянные авто-задачи из конфига + превью на одобрение
в отдельный ревью-канал с ✅/❌ + автогенерация карточек по таймеру.

**Architecture:** content-factory остаётся единственным «мозгом». Новый модуль
`orchestrator/auto.py` разворачивает авто-задачи из `config.yaml` в слоты `TaskQueue`
(идемпотентно, PK task_id+due_at). Превью confirm-потока шлётся в ревью-канал
(`TELEGRAM_REVIEW_CHANNEL_ID`, фолбэк — личка); после нажатия ✅/❌ бот заменяет кнопки
на «вердикт»-кнопку (editMessageReplyMarkup), чтобы канал был журналом ревью.

**Tech Stack:** Python 3.11+ (на деле 3.14), pydantic/httpx/PyYAML/decouple/pytest;
тесты — httpx.MockTransport, без сети/БД. Спека: `docs/superpowers/specs/2026-07-02-full-automation-design.md`.

---

### Task 1: Закоммитить готовый WIP «категория→mode»

WIP из прошлой сессии (см. docs/PLAN-category-mode.md) дописан и зелёный — фиксируем.

**Files:** уже изменены: `examples/config.example.yaml`, `src/content_factory/cards_run.py`,
`src/content_factory/config.py`, `src/content_factory/content/cards.py`,
`tests/test_cards.py`, `tests/test_config.py`.

- [ ] **Step 1: Прогнать тесты** — `python -m pytest -q` → Expected: `189 passed`
- [ ] **Step 2: Коммит**

```bash
git add examples/config.example.yaml src/content_factory/cards_run.py src/content_factory/config.py src/content_factory/content/cards.py tests/test_cards.py tests/test_config.py
git commit -m "feat(cards): авто-выбор mode карточки по category_id (карта в конфиге, override и default)"
```

### Task 2: Конфиг — `auto_tasks` + `telegram.review_channel_id`

**Files:**
- Modify: `src/content_factory/config.py` (TelegramConfig, AppConfig, load_config)
- Modify: `examples/config.example.yaml`
- Test: `tests/test_config.py`

- [ ] **Step 1: Написать падающий тест** (в конец tests/test_config.py)

```python
def test_auto_tasks_and_review_channel(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "telegram: {channel_id: '@chan', review_channel_id: '-100500'}\n"
        "auto_tasks:\n"
        "  - id: ac\n"
        "    filter: {categories: [2, 6, 7]}\n"
        "    count: 2\n"
        "    times: ['10:00', '14:00']\n",
        encoding="utf-8")
    cfg = load_config(p)
    assert cfg.telegram.review_channel_id == "-100500"
    assert cfg.auto_tasks == [{"id": "ac", "filter": {"categories": [2, 6, 7]},
                               "count": 2, "times": ["10:00", "14:00"]}]


def test_auto_tasks_default_empty(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("telegram: {channel_id: '@chan'}\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.auto_tasks == []
    assert cfg.telegram.review_channel_id == ""
```

- [ ] **Step 2: Убедиться, что падает** — `python -m pytest tests/test_config.py -q` → FAIL (нет атрибутов)
- [ ] **Step 3: Минимальная реализация** в `config.py`:
  - `TelegramConfig` — добавить поле `review_channel_id: str = ""`;
  - `AppConfig` — добавить поле `auto_tasks: list` (сразу после `telegram`, с комментарием
    «постоянные авто-задачи (сырые dict из yaml; разбор — orchestrator/auto.py)»);
  - в `load_config`: `review_channel_id=tg.get("review_channel_id", "") or ""` и
    `auto_tasks=d.get("auto_tasks", []) or []` в конструкторе AppConfig.
- [ ] **Step 4: Тесты зелёные** — `python -m pytest -q` → PASS (191)
- [ ] **Step 5: Обновить examples/config.example.yaml** (после блока `telegram:`):

```yaml
  review_channel_id: ""        # закрытый ревью-канал (превью с ✅/❌); пусто = в личку владельцу

auto_tasks:                    # постоянные авто-задачи: планировщик сам создаёт слоты на сегодня
  - id: "ac"                   # → task_id = auto-ac-<дата>
    filter: {categories: [2, 6, 7]}
    count: 2                   # серий за слот
    times: ["10:00", "14:00"]  # локальное время сервера
    # mode: "mcp"              # опц.; по умолчанию — авто по категории (cards.modes_by_category)
    # confirm: false           # опц.; по умолчанию true — через ревью-канал
```

- [ ] **Step 6: Коммит**

```bash
git add src/content_factory/config.py tests/test_config.py examples/config.example.yaml
git commit -m "feat(config): auto_tasks и telegram.review_channel_id"
```

### Task 3: `orchestrator/auto.py` — разворачивание авто-задач

**Files:**
- Create: `src/content_factory/orchestrator/auto.py`
- Test: `tests/test_auto.py`

- [ ] **Step 1: Написать падающие тесты** (`tests/test_auto.py`)

```python
from datetime import date
from content_factory.orchestrator.auto import materialize_auto_tasks
from content_factory.orchestrator.queue import TaskQueue

CFG = [{"id": "ac", "filter": {"categories": [2]}, "count": 2, "times": ["10:00", "14:00"]}]


def test_materialize_creates_slots_for_today(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    tasks = materialize_auto_tasks(CFG, date(2026, 7, 2), q)
    assert [t.id for t in tasks] == ["auto-ac-2026-07-02"]
    slots = q.all_slots()
    assert [(s.due_at, s.count, s.confirm) for s in slots] == [
        ("2026-07-02 10:00", 2, True), ("2026-07-02 14:00", 2, True)]
    assert slots[0].filter == {"categories": [2]}


def test_materialize_idempotent_and_keeps_done(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    materialize_auto_tasks(CFG, date(2026, 7, 2), q)
    q.mark_done("auto-ac-2026-07-02", "2026-07-02 10:00")
    materialize_auto_tasks(CFG, date(2026, 7, 2), q)      # повторный тик
    slots = q.all_slots()
    assert len(slots) == 2                                # дублей нет
    assert {s.due_at: s.status for s in slots} == {
        "2026-07-02 10:00": "done", "2026-07-02 14:00": "pending"}


def test_materialize_next_day_new_slots(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    materialize_auto_tasks(CFG, date(2026, 7, 2), q)
    materialize_auto_tasks(CFG, date(2026, 7, 3), q)
    assert len(q.all_slots()) == 4                        # у каждого дня свой task_id


def test_materialize_invalid_config_raises(tmp_path):
    q = TaskQueue(tmp_path / "q.db")
    try:
        materialize_auto_tasks([{"id": "x", "count": 1}], date(2026, 7, 2), q)
        assert False, "ждали ValueError"
    except ValueError as e:
        assert "times" in str(e)
```

- [ ] **Step 2: Убедиться, что падают** — `python -m pytest tests/test_auto.py -q` → FAIL (модуля нет)
- [ ] **Step 3: Реализация** `src/content_factory/orchestrator/auto.py`:

```python
"""Постоянные авто-задачи (полный автомат). Конфиг auto_tasks (yaml) на каждом тике
планировщика разворачивается в слоты «на сегодня»: task_id = auto-<id>-<дата>, поэтому
каждый день появляются свежие слоты, а повторный тик ничего не дублирует
(TaskQueue.add — INSERT OR IGNORE по (task_id, due_at), done-статусы сохраняются).
confirm по умолчанию ВКЛ — всё идёт через ревью-канал владельца."""
from __future__ import annotations
from datetime import date
from content_factory.orchestrator.tasks import Task


def materialize_auto_tasks(auto_cfgs: list, today: date, queue) -> list[Task]:
    tasks = []
    for d in auto_cfgs or []:
        aid = d.get("id")
        if not aid:
            raise ValueError("auto_tasks: у задачи нет 'id'")
        if d.get("count") is None:
            raise ValueError(f"auto_tasks {aid}: не указан 'count'")
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
```

- [ ] **Step 4: Тесты зелёные** — `python -m pytest -q` → PASS
- [ ] **Step 5: Коммит** — `git add src/content_factory/orchestrator/auto.py tests/test_auto.py && git commit -m "feat(orchestrator): materialize_auto_tasks — постоянные авто-задачи из конфига"`

### Task 4: scheduler_run — авто-задачи на тике + превью в ревью-канал

**Files:**
- Modify: `src/content_factory/orchestrator/scheduler_run.py`
- Test: `tests/test_wiring.py`

- [ ] **Step 1: Падающий тест** (в tests/test_wiring.py; `_http` научить копить тела запросов)

```python
def _http2(calls, bodies):
    def handler(req):
        calls.append(req.url.path)
        bodies.append(req.read())
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.telegram.org")


def test_wiring_confirm_preview_goes_to_review_channel(tmp_path):
    cfg, groups = _setup(tmp_path)
    cfg.telegram.review_channel_id = "-100777"
    calls, bodies = [], []
    ps, cs = PublishState(cfg.state.db), ConfirmStore(cfg.state.db)
    ctx = build_context(cfg, "TOK", "999", ps, cs, http=_http2(calls, bodies),
                        review_chat=cfg.telegram.review_channel_id)
    q = TaskQueue(tmp_path / "q.db")
    q.add(Task(id="t", filter={}, count=5, confirm=True))
    run_slot(q.due("2999-01-01 00:00")[0], groups, ctx)
    photo_bodies = [b for p, b in zip(calls, bodies) if "sendPhoto" in p]
    assert photo_bodies and b"-100777" in photo_bodies[0]   # превью ушло в ревью-канал
```

- [ ] **Step 2: Убедиться, что падает** — `python -m pytest tests/test_wiring.py -q` → FAIL (нет параметра review_chat)
- [ ] **Step 3: Реализация** в `scheduler_run.py`:
  - `build_context(…, review_chat: str = "")`: в `confirm()` превью слать в
    `review_chat or owner_chat` (оба вызова publish_post внутри confirm); алерты — по-прежнему owner_chat.
  - `main()`: после `q = TaskQueue(...)` добавить
    `materialize_auto_tasks(cfg.auto_tasks, date.today(), q)` (import из orchestrator.auto,
    `from datetime import date, datetime`);
  - `main()`: `ctx = build_context(…, review_chat=config("TELEGRAM_REVIEW_CHANNEL_ID", cfg.telegram.review_channel_id))`.
- [ ] **Step 4: Все тесты зелёные** — `python -m pytest -q` → PASS (старые wiring-тесты без review_chat работают: фолбэк owner_chat)
- [ ] **Step 5: Коммит** — `git commit -m "feat(scheduler): авто-задачи на каждом тике + превью confirm в ревью-канал"`

### Task 5: bot/run.py — вердикт вместо кнопок после ✅/❌

После нажатия в ревью-канале: answerCallbackQuery (тост) + editMessageReplyMarkup — кнопки
заменяются одной «вердикт»-кнопкой (✅ Опубликовано / ❌ Отклонено), подпись и форматирование
не трогаем. Отдельное sendMessage-эхо в канал больше не шлём (шумит в журнале ревью);
callback_data "noop" игнорируется.

**Files:**
- Modify: `src/content_factory/bot/run.py`
- Test: `tests/test_bot_run.py`

- [ ] **Step 1: Падающий тест** (tests/test_bot_run.py)

```python
def test_finalize_preview_replaces_buttons_with_verdict():
    reqs = []

    def handler(req):
        reqs.append((req.url.path, req.read()))
        return httpx.Response(200, json={"ok": True})
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.telegram.org")
    cq = {"message": {"chat": {"id": -100777}, "message_id": 42}}
    botrun.finalize_preview(http, "TOK", cq, "✅ опубликовано: k1")
    path, body = reqs[0]
    assert path == "/botTOK/editMessageReplyMarkup"
    assert b"42" in body and "✅ опубликовано: k1".encode() in body


def test_finalize_preview_no_message_is_noop():
    http = httpx.Client(transport=httpx.MockTransport(
        lambda req: (_ for _ in ()).throw(AssertionError("не должно быть запросов"))),
        base_url="https://api.telegram.org")
    botrun.finalize_preview(http, "TOK", {}, "✅")   # без message — тихо выходим
```

- [ ] **Step 2: Убедиться, что падает** — `python -m pytest tests/test_bot_run.py -q` → FAIL (нет finalize_preview)
- [ ] **Step 3: Реализация** в `bot/run.py` (import json вверху):

```python
def finalize_preview(http, token: str, cq: dict, verdict: str) -> None:
    """После ✅/❌ в ревью-канале: заменить кнопки превью одной «вердикт»-кнопкой
    (подпись/форматирование не трогаем — канал остаётся журналом ревью)."""
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    if not (chat_id and message_id):
        return
    kb = json.dumps({"inline_keyboard": [[{"text": verdict[:60], "callback_data": "noop"}]]},
                    ensure_ascii=False)
    try:
        http.post(f"{TG_API}/bot{token}/editMessageReplyMarkup",
                  data={"chat_id": chat_id, "message_id": message_id, "reply_markup": kb})
    except httpx.HTTPError:
        pass
```

  В main-цикле обработки callback: в начале `if cq.get("data") == "noop": … answerCallbackQuery
  и continue`; после `reply = handle_callback(…)` вместо sendMessage-эха вызвать
  `finalize_preview(http, token, cq, reply)` (answerCallbackQuery оставить).
- [ ] **Step 4: Все тесты зелёные** — `python -m pytest -q` → PASS
- [ ] **Step 5: Коммит** — `git commit -m "feat(bot): вердикт вместо кнопок после ✅/❌ (журнал в ревью-канале)"`

### Task 6: Деплой-инструкция (без выполнения — внешнее, по ОК владельца)

**Files:**
- Modify: `deploy/DEPLOY.md` — раздел «Полный автомат»:
  1. залить код (tar+ssh), `python -m pytest -q` на сервере;
  2. `.env` — добавить `TELEGRAM_REVIEW_CHANNEL_ID=<id ревью-канала>` (владелец создаёт канал,
     бот — админ);
  3. `config/config.yaml` — секция `auto_tasks` (темп согласовать с владельцем);
  4. `systemctl enable --now cf-cards.timer` (автогенерация карточек);
  5. пилот: авто-задача на 1–2 поста с публикацией в тестовый канал → ОК владельца → боевой.

- [ ] **Step 1: Дописать DEPLOY.md, коммит** — `git commit -m "docs(deploy): включение полного автомата"`

---

## Follow-up (отдельные планы, после сдачи этого)

- **Подпроект 2** (репо agent_convert_foto_rituailb2b2): research-задачи — план писать после
  изучения vps_api.py/vps_bot.py/agent.py в том репо.
- **Подпроект 3** (content-factory): Excel-источник + `/make` — план после примера прайса владельца.
