# Фаза 1 — статус и хендофф (для следующей сессии)

Снимок состояния «Контент-завода» после первой сессии разработки. Читать вместе с
[PHASE-1-PLAN.md](PHASE-1-PLAN.md), [ARCHITECTURE.md](ARCHITECTURE.md),
[HANDOFF-FROM-AVITO-BRIDGE.md](HANDOFF-FROM-AVITO-BRIDGE.md), [../CLAUDE.md](../CLAUDE.md).

## Что сделано: M0–M5 (логическое ядро Фазы 1) — `pytest` 135 passed

| M | Что | Файлы |
|---|-----|-------|
| M0 | Каркас + перенос ядра из avito-bridge (пакет `content_factory`) | `pyproject.toml`, `src/content_factory/{models,config,cards_pipeline,cards_run}.py`, `ingest/*`, `pricing/*`, `catalog/series.py`, `content/{sizing,cards,descriptions}.py` |
| M1 | Краткая подпись для Telegram (≤1024) | `content/render.py::render_caption` |
| M2 | Детерминированная ревизия (без LLM) | `review/rules.py::review` + `ReviewItem` |
| M3 | Публикатор Telegram (sendPhoto, идемпотентность, ретраи) | `publish/telegram.py::publish_post` + `PublishState` |
| M4 | Оркестратор: задачи/очередь/планировщик | `orchestrator/{tasks,queue,scheduler}.py` |
| M5 | Вход задач: YAML-планы + парсер бот-команд | `orchestrator/plans.py`, `bot/commands.py` |

Сквозной путь собран и протестирован:
**команда боту / YAML → очередь слотов (по расписанию) → планировщик → выбор N невыпущенных
серий → цена → подпись → гейт `require_card` → ревизия → (confirm / публикация sendPhoto)**.
Анти-дубль по `group.key` (PublishState), авто-ретрай карточек делегирован в `cards_pipeline`.

Тесты по модулям: `tests/test_*` — `config, render, review, publish, tasks, queue, scheduler,
plans, commands` + перенесённые тесты ядра. Запуск: `python -m pytest -q` (из корня репо).

## Решения владельца, принятые в этой сессии
1. **Формат поста (caption):** заголовок «Бренд+серия+тип» с мощностью/площадью + 1 строка
   пользы + **цена** + **CTA**. **Без хэштегов и без ссылки на сайт** (как в Avito). Реализовано
   в `render_caption`; ручной override на серию поддержан (manifest, как в avito-bridge).
2. **Режим публикации:** **авто + алерт при fail**, НО на задаче есть флаг **`confirm`**
   (human-in-the-loop) — используем на пилоте, потом снимаем. В `Task.confirm` и логике
   `scheduler.run_slot` (при `confirm=True` пост уходит в очередь подтверждения, не публикуется).

## Что осталось: M6 — деплой + таймеры + пилот (НЕ начато)
M6 — единственный «внешний» милстоун (нужен сервер, секреты, ОК владельца на отправку).
Делится на:

### (a) Код-обвязка (можно делать без секретов/доступа)
- `orchestrator/scheduler_run.py` — точка входа для таймера: load_config → ingest (как в
  `cards_run.py`) → `group_by_series` → собрать `PipelineContext` (publish=telegram,
  published_keys=PublishState, alert=Bot API sendMessage владельцу, submit_cards=лог/wake —
  карточки генерит отдельный таймер `cards_run`) → `load_plans_into_queue(tasks/)` →
  `run_due(now)`.
- `bot/run.py` — тонкий long-poll раннер (httpx `getUpdates` → `handle_command` → ответ).
  Команда `/approve <key>` для confirm-пилота (публикует отложенный пост) — ДОПИСАТЬ
  (в M5 заложены `confirm`/`awaiting`, сам approve-стор и публикация по approve — TODO).
- `config/config.yaml` (несекретный, из `examples/config.example.yaml`) + `config/card_modes.json`.
- `deploy/`: unit-файлы systemd `cf-cards.{service,timer}` (мост к фотоагенту, = `cards_run`),
  `cf-scheduler.{service,timer}` (= `scheduler_run`), `cf-bot.service` (= `bot/run`); `DEPLOY.md`.
- Возможно: `PublishState.published_keys()` (множество всех ключей) для контекста планировщика.

### (b) Деплой + пилот (внешнее, по ОК владельца)
- Деплой tar+ssh (scp на VPS не работает — см. CLAUDE.md/HANDOFF). `.env` на сервере: креды БД
  (из `/opt/avito-bridge/.env`), фотоаген (`FOTOGEN_*`), `TELEGRAM_BOT_TOKEN`, каналы.
- Пилот: задача «N кондиционеров, mode=mcp, завтра 10:00» → **в тестовый канал/личку** прошёл
  полный цикл → перевод на боевой канал по явному ОК.

## Открытые вопросы к владельцу (нужны для M6)
1. **Каналы:** id/@username **боевого** и **тестового** канала; токен бота — server-side в `.env`.
2. **Темп:** постов/день, интервал между постами, окна времени (для конфига и расписаний).
3. **Первая категория после кондиционеров** (владелец упоминал холодильники) — фильтр в oasis.
4. Подтвердить дефолт confirm-режима на пилоте (вкл) и момент снятия.

## Как продолжить (быстрый старт следующей сессии)
1. `python -m pytest -q` — должно быть зелёным (135).
2. Прочитать этот файл + PHASE-1-PLAN (M6).
3. Сделать M6(a) код-обвязку (TDD/смоук-импорт), затем по ОК — M6(b) деплой+пилот.

## Грабли среды (подтверждено в этой сессии)
- Python в среде — 3.14; зависимости (`pydantic/httpx/yaml/decouple/pytest`) доступны.
- Консоль Windows — cp1251: для печати юникода в скриптах ставить `PYTHONIOENCODING=utf-8`.
- `pytest` настроен на `pythonpath=["src"]` (см. `pyproject.toml`) — запускать из корня репо.
- Эталон движка: `../Avito/avito-bridge/` (т.е. `Codex/Avito/avito-bridge`). Не править — копировать.
