# Деплой Контент-завода (VPS 212.116.115.150)

Сервер: тот же VPS, что фотоагент (ritualb2b на `127.0.0.1:8765`); переезд 2026-07-19 —
старый 213.109.202.45 выключен (сервисы там disabled, не чинить).
Целевой каталог: `/opt/content-factory`. SSH: `ssh -i ~/.ssh/id_ritualb2b_claude root@212.116.115.150`.
Деструктив/внешнее (systemd, реальная отправка в канал) — с подтверждения владельца.

## 0. Предпосылки
- БД oasis (`oasis-db-1`, сеть `oasis_default`) и очередь фотоагента (`/root/ritualb2b/`) уже на VPS.
- Карточки раздаются nginx из `/opt/oasis/staticfiles/` → `https://splithome.ru/static/...`
  (для cf — подпапка `cf-cards`). nginx НЕ правим без необходимости.
- Бот **@Sendpr1ce_bot** — администратор канала «СплитХаб.ру» (проверено `getChatMember`).

## 1. Выкладка кода (scp на этом VPS не работает — tar+ssh через stdin)
```bash
cd content-factory
tar -czf /tmp/cf.tgz src config deploy pyproject.toml requirements.txt
ssh -i ~/.ssh/id_ritualb2b_claude root@212.116.115.150 'mkdir -p /opt/content-factory && cat > /tmp/cf.tgz' < /tmp/cf.tgz
ssh -i ~/.ssh/id_ritualb2b_claude root@212.116.115.150 'cd /opt/content-factory && tar -xzf /tmp/cf.tgz'
```
(Проверять, что `/tmp/cf.tgz` существует локально — иначе цепочка с `&&` прервётся до `tar -xzf`.)

## 2. venv + зависимости (на сервере)
```bash
cd /opt/content-factory
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir -p state tasks /opt/oasis/staticfiles/cf-cards
```

## 3. .env (НЕ из git — создать на сервере)
Скопировать структуру из `.env.example`. Креды БД и токен фотоагента взять из
`/opt/avito-bridge/.env` (`DB_*`, `FOTOGEN_API_TOKEN`, `FOTOGEN_QUEUE_DB`, `FOTOGEN_OUTPUT_DIR`).
Telegram: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID` (боевой), `TELEGRAM_OWNER_CHAT_ID` (личка
владельца для превью/алертов). НЕ коммитить, не эхоить.

## 4. Дымовой прогон (read-only / без реальных постов)
```bash
.venv/bin/python -m pytest -q                      # на сервере не обязателен, но желателен
.venv/bin/python -m content_factory.cards_run      # сгенерит/добёрет карточки (мост к фотоагенту)
# scheduler без дозревших слотов просто напишет «дозревших слотов нет»
.venv/bin/python -m content_factory.orchestrator.scheduler_run
```

## 5. systemd (с подтверждения владельца)
Скопировать unit-файлы и включить:
```bash
cp deploy/cf-*.service deploy/cf-*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now cf-cards.timer cf-scheduler.timer cf-bot.service
systemctl status cf-bot.service
journalctl -u cf-scheduler.service -n 50 --no-pager
```
- `cf-cards.timer` — каждые 30 мин: мост к фотоагенту (submit+collect+wake).
- `cf-scheduler.timer` — каждые 5 мин: ловит дозревшие слоты, проводит конвейер.
- `cf-bot.service` — long-poll бот (команды владельца, `/approve`).

## 6. Пилот (в БОЕВОЙ канал — только с confirm!)
Отдельного тестового канала нет → пилотную задачу ставить **обязательно с `confirm`**:
бот пришлёт владельцу превью каждого поста, публикация — по `/approve <key>`.
```
# в личке боту:
/plan 5 кондиционеры завтра 10:00 mode=mcp confirm
# когда подготовится: придёт превью → /approve <key> (или /reject <key>)
/pending     # список ожидающих
/status      # что в очереди
```
После успешного прогона и решения владельца — ставить задачи без `confirm` (авто-публикация,
алерт только при fail-ревизии).

## 7. Полный автомат (2026-07-02, спека docs/superpowers/specs/2026-07-02-full-automation-design.md)
Включение по шагам (каждый внешний шаг — по ОК владельца):
1. Выложить код (п. 1) и прогнать тесты на сервере: `.venv/bin/python -m pytest -q`.
2. Владелец создаёт **закрытый ревью-канал**, добавляет @Sendpr1ce_bot админом, присылает id →
   в `.env`: `TELEGRAM_REVIEW_CHANNEL_ID=<id>`. (Пусто = превью в личку, как раньше.)
3. В `config/config.yaml` — секция `auto_tasks` (пример в `examples/config.example.yaml`;
   темп: count × times = постов/день, согласовать с владельцем). `confirm` не выключать,
   пока владелец хочет ручную перепроверку.
4. Убедиться, что `cf-cards.timer` включён (автогенерация карточек):
   `systemctl enable --now cf-cards.timer` — и что генератор (десктоп/ноут) доступен
   (см. Task 7 в репо агента: десктоп обновить до failover-ветки ДО одновременной работы).
5. Пилот: авто-задача с малым темпом (count=1, 1–2 времени) → превью падают в ревью-канал →
   владелец жмёт ✅/❌ (после нажатия кнопки заменяются «вердиктом»). Публикация уходит в
   `TELEGRAM_CHANNEL_ID`.
6. Рестарт сервисов после выкладки: `systemctl restart cf-bot.service` (+ таймеры сами
   подхватят новый код на следующем тике).

## Откат
`systemctl disable --now cf-*.timer cf-bot.service` — остановит автоматику; код/данные остаются.
Полный автомат откатывается удалением секции `auto_tasks` из `config/config.yaml`
(уже созданные на сегодня слоты можно снять: `/cancel auto-<id>-<дата>`).
