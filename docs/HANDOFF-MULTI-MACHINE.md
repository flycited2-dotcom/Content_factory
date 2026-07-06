# Хендофф: разведение агентов на нескольких машинах (desktop + laptop)

Тематический хендофф (не дневной). Читать вместе с [HANDOFF-2026-07-05.md](HANDOFF-2026-07-05.md),
где описан генератор карточек и флаги в `queue.db` на VPS.

## Проблема
Watchdog поднимает генератор карточек (`remote_agent.py`, гонит ChatGPT через Chrome CDP)
**и на десктопе, и на ноуте независимо**. Без разведения оба инстанса стучат в один
ChatGPT/очередь одновременно → дубли, гонки, лишний расход. Плюс `cf-bot` (long-poll):
два инстанса на одном токене → Telegram 409 Conflict.

## Решение: реестр машин + «дороги»
[config/machines.yaml](../config/machines.yaml) — общая карта машин (в git, видят обе).
- Ключ машины — **MachineGuid** (Windows, стабильный): агент читает свой GUID в рантайме
  и находит себя в реестре по `machine_guid`.
- `command_key` — имя флага start/stop в `queue.db` на VPS. Каждая машина слушает **только
  свой** `command_key`, поэтому дороги не пересекаются:
  - laptop → `agent_command_laptop` (уже используется на VPS, см. HANDOFF-2026-07-05)
  - desktop → `agent_command_desktop` (**НАДО завести флаг на VPS** — по аналогии с laptop)
- `runs_bot` — держать `true` только на ОДНОЙ машине (иначе 409 у cf-bot).

## Сделано (desktop, эта сессия)
- Репо склонирован на десктоп: `C:\Users\TLT-1\Documents\GitHub\content-factory`.
- Заведён `config/machines.yaml`; десктоп прописан:
  `machine_guid=4557e2b1-f540-4dc1-a4db-f733c096ab11`, `hostname=DESKTOP-A59E7AI`,
  `role=card-agent`, `command_key=agent_command_desktop`, `runs_bot=false`.
- Добавлен [scripts/register-machine.ps1](../scripts/register-machine.ps1) — самозаполнение
  блока машины (GUID/hostname/role/runs_bot).

## Сделано (laptop, сессия 2026-07-06) — Phase 1 спеки лейнов
- **Ноут зарегистрирован** в machines.yaml (GUID f91c394d…, WIN-DO3I2LOARD0).
- **Ветки агента сведены**: рабочая ветка влита в `main` (репо агента, c4785bf) —
  дальше обе машины работают от `main`.
- **Атомарный claim + аренда + /api/requeue** (репо агента, 79b2c41, ЗАДЕПЛОЕНО на VPS):
  `/api/next-job` захватывает задачу одним `UPDATE…RETURNING` (+`?lane=` в `claimed_by`);
  processing старше `AGENT_JOB_LEASE_SECONDS` (45 мин) возвращается в pending.
  Эксклюзивность работы машин теперь обеспечивает ОЧЕРЕДЬ, а не command_key.
- **vps_bot знает desktop-ключ**: `agent_command_desktop` в `_AGENT_FLAG_KEYS` и в
  эксклюзивных кнопках (двухканально с legacy `agent_command`) — десктоп-вотчдог
  может переходить на `worker=desktop`, не оглохнув для кнопок.

## Уточнения после ревью (важно для следующих фаз)
- **«Завести флаг в queue.db» НЕ нужно** — флаги создаются на лету при постановке команды.
- **`command_key` ≠ эксклюзивность**: он разводит только доставку команд. Не строить
  на нём взаимоисключение — это делает атомарный claim (и старый лиз-слой `workers`,
  судьбу которого решить в Phase 6).
- **`runs_bot` — ложная дилемма**: cf-bot живёт на VPS (systemd), Windows-машины его
  не запускают. Держать false у всех.
- **lanes.json (Phase 3) ОБЯЗАН иметь привязку к машине** (`machine:` + резолв по
  MachineGuid через этот реестр) — иначе оба вотчдога поднимут все дорожки и
  задвоят аккаунты.

## Сделано (laptop, продолжение 2026-07-06) — Phase 2-3
- **lanes.json** (репо агента, 117ceff): карта машин по MachineGuid + дорожки с
  `machine:`-привязкой; `my_lanes()` отдаёт только свои — проверено на ноуте
  (→ `['laptop-a1']`). project_urls: литерал или `env:ИМЯ` (URL в .env).
- **start_chrome.bat** параметризован (`%1`=порт, `%2`=профиль; дефолты прежние).
- **agent.py / remote_agent.py**: cdp_url/project_url per-lane, `LANE_ID` (env/CLI),
  лог `remote_agent_{lane}.log`, `?lane=` в next-job и heartbeat. Без LANE_ID —
  поведение прежнее (одна дорожка).
- **vps_api heartbeat per-lane** (таблица `lane_heartbeat`) — ЗАДЕПЛОЕНО на VPS.

## Сделано (laptop, продолжение-2 2026-07-06) — Phase 5
- **agent_watchdog на N дорожек** (a7bf15d): команда из TG применяется ко всем
  дорожкам машины (`my_lanes()`); per-lane Chrome/агент/state-файл/само-
  восстановление; kill по LANE_ID в cmdline; legacy-режим без lanes.json.
  ⚠️ На ноуте живёт СТАРЫЙ процесс вотчдога — новый код включится после
  перезапуска вотчдога (kill процесса → планировщик поднимет за ≤5 мин).
- **vps_bot: per-lane статус** («Дорожки: 🟢 laptop-a1: N сек») — ЗАДЕПЛОЕНО.

## Сделано (laptop, продолжение-3 2026-07-06) — Phase 4 (часть) + Phase 6
- **Аренда по АККАУНТУ** (агент, 18e3eb0): `Lane.account` — арендный ключ лиза
  (laptop-a1/desktop-a1 = acc1 → на очереди работает одна машина; acc2 будет
  молотить параллельно). Решение «судьбы workers-лиза»: ОСТАВЛЕН, ключ = аккаунт.
- **UploadLimitError → requeue** (не fail): попытка конвейера не сжигается,
  задачу добирает другая дорожка / та же после кулдауна; фолбэк в fail.
- **wake_agent (content-factory, 2a52b2c, ЗАДЕПЛОЕНО)**: бродкаст на все ключи —
  раньше будил только глобальный (ноут с адресным флагом не просыпался).

## Сделано (laptop, продолжение-4 2026-07-06 ~13:15) — АКК2 ВКЛЮЧЁН И РАБОТАЕТ
- Владелец создал проекты в акк2 (Chrome :9334): mcp+kbt — ОДИН общий проект
  (как в акк1: MCP==KBT==CONDITIONER, проверено сравнением .env), research —
  отдельный. URL в .env (`*_PROJECT_URL_ACC2`), laptop-a2 `enabled: true`
  (агент 533501a). Вотчдог перезапущен (LANES читаются при старте!) → поднял
  оба Chrome и двух агентов по команде start.
- **Найден и починен блокер — лиз был ГЛОБАЛЬНЫМ (агент 8f7f7e1, ЗАДЕПЛОЕНО)**:
  /api/worker/lease выбирал одного активного среди ВСЕХ воркеров → laptop-a2
  висела в вечном standby («lease=acc2, активен другой») при живом laptop-a1.
  Теперь: worker_id = уникальный claim'ер (id дорожки), account = группа аренды,
  активный выбирается ВНУТРИ группы; пустой account = legacy-группа (старый
  failover desktop/laptop сохранён). Таблица workers мигрируется ALTER'ом.
- **Параллельность подтверждена вживую**: 2 mcp-задачи (628/629) разобраны
  claimed_by=laptop-a1 и laptop-a2 одновременно, обе дорожки бьют lane_heartbeat.

## Открытые вопросы / TODO
- Phase 0 + детект лимита ГЕНЕРАЦИИ (Phase 4) — ждёт реальной модалки лимита на
  акк1 (снять DOM+скриншот через `agent._dump_page_state`). Лимит ЗАГРУЗОК уже
  обработан (UploadLimitError + кулдаун + requeue).
- Per-lane кнопки start/stop в vps_bot — отложено (машинного уровня + enabled-флага
  в lanes.json пока достаточно).
- Десктоп: `git checkout main && git pull` (владелец, при доступе к машине).
- result_sender: пустой output-файл (0 байт) зацикливает отправку — код не чинился
  (2026-07-06 job 324 погашен вручную result_sent=1).
- Строки-сироты в workers (acc1/acc2/laptop с account='') — от кода до фикса лиза,
  протухают по TTL 15 мин, не мешают; можно почистить при случае.
