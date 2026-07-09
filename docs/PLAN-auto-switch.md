# Спек: выключатель авто-контента (`/auto`) — 2026-07-09

Задача 2 бэклога [HANDOFF-2026-07-07-EVENING](HANDOFF-2026-07-07-EVENING.md).
Владелец: «постоянно гонится история с контентом, не пойму, кто запустил».
Это auto_tasks из config.yaml (am/day/eve, 32 превью/день по кондиционерам,
cf-scheduler.timer каждые 5 мин). Нужен выключатель.

## Решения владельца (2026-07-09)

- Режим: **выключить сейчас, включать вручную**.
- Механизм: **команда `/auto` в боте** (флаг в state-БД; без SSH).
- Дефолт: **нет записи в БД = ВЫКЛЮЧЕНО** — автомат встаёт сразу после деплоя,
  до всякого `/auto on`.

## Дизайн

### 1. Флаг в state-БД (`orchestrator/auto.py`)
Таблица `settings(key TEXT PRIMARY KEY, value TEXT)` в `cfg.state.db`
(создание — CREATE TABLE IF NOT EXISTS, как у остальных сторов).

- `auto_enabled(db) -> bool` — `settings['auto_enabled'] == '1'`; нет записи → False.
- `set_auto_enabled(db, on: bool)` — INSERT OR REPLACE `'1'`/`'0'`.

### 2. Планировщик — саморегулируемый (`auto.py::maybe_materialize`)
`scheduler_run.main()` зовёт вместо `materialize_auto_tasks` обёртку:

```python
def maybe_materialize(auto_cfgs, today, queue, db) -> list[Task]:
    # флаг ВЫКЛ → не материализовать И отменить pending auto-* слоты
    # (страховка: даже уже созданные не исполнятся); ручные задачи не трогаем
    # флаг ВКЛ → materialize_auto_tasks как раньше
```

`materialize_auto_tasks` не меняется (тесты test_auto.py не трогаем).

### 3. Очередь: отмена/воскрешение auto-слотов (`orchestrator/queue.py`)
Соглашение: авто-слоты имеют `task_id` с префиксом `auto-` (auto.py:22).

- `cancel_auto() -> int` — pending + `task_id LIKE 'auto-%'` → cancelled.
- `uncancel_auto(after: str) -> int` — cancelled + `LIKE 'auto-%'` +
  `due_at > after` → pending. **Только будущие**: включение в 14:00 не должно
  залпом исполнить утренние слоты «догоном».

### 4. Команда `/auto` (`bot/commands.py` + `bot/run.py`)
Инжект `auto_fn(arg: str | None) -> str` в `handle_command`
(паттерн `excel_fn`); замыкание собирается в `run.py` (знает cfg и state.db).

- `/auto` — статус: `▶️ ВКЛЮЧЁН` / `⏸ ВЫКЛЮЧЕН` + расписание из
  `cfg.auto_tasks` (id, times, count) + авто-слоты сегодня (pending/done) +
  подсказка `/auto on|off`.
- `/auto off` — `set_auto_enabled(False)` + `cancel_auto()` немедленно
  (не ждать тика): `⏸ авто-контент выключен, отменено N слотов`.
- `/auto on` — `set_auto_enabled(True)` + `uncancel_auto(now)`:
  `▶️ авто-контент включён, сегодня ещё N слотов` (новые дни материализует
  планировщик следующим тиком, ≤5 мин).
- `auto_fn` нет (CLI-вызов без бота) → `❌ авто-контент недоступен`.

### 5. Строка в `/status` (`commands.py::_status`)
Инжект `auto_state_fn() -> bool | None` (None = авто не настроено — строки нет):
`🤖 Авто-контент: ▶️ включён (/auto off)` / `⏸ выключен (/auto on)`.

## Тесты (TDD, tests/test_auto.py + test_commands.py)

- флаг: дефолт False; set/get; независимые ключи settings не ломаются.
- `maybe_materialize`: ВЫКЛ → пусто + pending auto-* отменены, ручные целы;
  ВКЛ → слоты созданы (как materialize).
- `cancel_auto`/`uncancel_auto`: только `auto-%`; uncancel — только `due_at > after`;
  done/cancelled ручные не трогаются.
- `/auto` статус/on/off тексты; `/auto` без auto_fn; строка в `/status`
  (вкл/выкл/None).

## Готово, когда

- Все тесты зелёные локально.
- Деплой на VPS (tar+ssh, по явному ОК владельца): рестарт `cf-bot`
  (long-polling; cf-scheduler — таймер, подхватит код сам).
- На проде: `/auto` отвечает `⏸ ВЫКЛЮЧЕН`, `/status` показывает строку
  автомата, к следующему тику pending auto-* слоты отменены.

## Вне скоупа (YAGNI)

Пауза «до даты», per-задачные выключатели (am/day/eve по отдельности),
правка расписания из бота — по запросу владельца отдельно.
