# Деплой Контент-завода (VPS 213.109.202.45)

Сервер: HestiaCP/Docker, тот же VPS, что avito-bridge и фотоагент (ritualb2b на `127.0.0.1:8765`).
Целевой каталог: `/opt/content-factory`. SSH: `ssh -i ~/.ssh/climat_simf_deploy root@213.109.202.45`.
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
ssh -i ~/.ssh/climat_simf_deploy root@213.109.202.45 'mkdir -p /opt/content-factory && cat > /tmp/cf.tgz' < /tmp/cf.tgz
ssh -i ~/.ssh/climat_simf_deploy root@213.109.202.45 'cd /opt/content-factory && tar -xzf /tmp/cf.tgz'
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

## Откат
`systemctl disable --now cf-*.timer cf-bot.service` — остановит автоматику; код/данные остаются.
