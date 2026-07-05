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

## Что сделать на НОУТЕ (следующий шаг владельца)
1. Подтянуть репо: `git pull origin master`.
2. Зарегистрировать ноут (из корня репо):
   `pwsh scripts/register-machine.ps1 -Name laptop -Role card-agent`
3. Закоммитить и запушить: `git add config/machines.yaml && git commit -m "chore(ops): реестр машин — laptop" && git push origin master`.

## Открытые вопросы / TODO
- **VPS-флаг `agent_command_desktop`** — завести в `queue.db` (сейчас есть только laptop).
- **Watchdog должен читать реестр**: определять свой GUID → свой `command_key` → слушать только
  его. Сейчас код это ещё не делает (реестр заведён, потребитель — нет).
- **Распределённый lock** на случай, если обе машины возьмутся за одну задачу (обсуждалось —
  через git-remote/VPS). Пока не реализовано.
- **Стратегия слияния веток** между машинами — отложена («решим позже»).
- Решить, какая машина держит `runs_bot=true`.
