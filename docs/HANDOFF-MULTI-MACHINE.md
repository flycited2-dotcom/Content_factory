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

## Открытые вопросы / TODO
- Phase 0 (фикстура модалки лимита генерации) — ждёт реального лимита на акк1.
- Включение акк2 (владелец): проекты+эталоны в ChatGPT-акк2 → `start_chrome.bat 9334
  chrome_profile_acc2` → логин → env-URL (`*_PROJECT_URL_ACC2`) в .env →
  `enabled: true` у laptop-a2 в lanes.json.
- Phase 4 (детект лимита генерации + остывание; в cooldown слать heartbeat).
- Phase 5 (вотчдог N дорожек из my_lanes(); статус per-lane в vps_bot).
- Phase 6 (судьба лиз-слоя `workers`; wake_agent в content-factory).
- Десктоп: `git checkout main && git pull` в репо агента (ветки сведены).
