# Передача наработок из avito-bridge → Контент-завод

Здесь всё, что уже построено и обкатано, с конкретными файлами, доступами и граблями. Эталонный код:
`../Avito/avito-bridge/` (отдельный git-репо: github `flycited2-dotcom/avito_agent_load`, bare на VPS
`/opt/git/avito-bridge.git`). Развёрнут на VPS `/opt/avito-bridge` (venv, systemd-таймеры). **Не править его —
копировать/адаптировать.**

---

## 1. Источник контента — PostgreSQL «oasis» (сайт splithome)
- БД сайта SplitHome. Контейнер `oasis-db-1` на VPS **213.109.202.45**, сеть `oasis_default`.
  Креды server-side: `/opt/avito-bridge/.env` (`DB_HOST=172.30.0.6`, `DB_PORT`, `DB_NAME=oasis_db`,
  `DB_USER=oasis_user`, `DB_PASSWORD=…`). Не эхоить креды в чат.
- Таблицы: `catalog_product` (title, series, category_id, btu_calc, price_wholesale, source, nc_code),
  `catalog_brand`, `stock_stock` (warehouse, quantity, price_base), `catalog_productimage` (url, order),
  `catalog_producttech` + `catalog_techspec` (ТТХ по nc_code).
- **4 поставщика** (`source`): `rusklimat`, `daichi`, `breeze` (Бриз), `jac` (без API, из JSON
  `jac_stock_latest.json`, скрапер).
- Пилот: склад **«Симферополь»**, категории 2/6/7 (кондиционеры), `btu_calc>0`. Для других категорий —
  свой фильтр (категория/исключения по названию).
- **Цена = опт × (1+наценка%) → округление ВВЕРХ до …90.** Опт по поставщику: Daichi→`price_wholesale`;
  Бриз→Breez API по nc_code (fallback БД); Русклимат→`stock_stock.price_base`→`price_wholesale`; JAC→из JSON.

**Код (переиспользовать почти как есть):**
- `ingest/oasis_db.py` — `CRIMEA_QUERY`/`TECH_QUERY`, `fetch_raw_products(dsn, warehouse, cats, deny)`.
- `ingest/normalize.py` — `to_offer`, `is_conditioner`/`CatalogFilter`, `content_hash`.
- `ingest/title_parse.py` — парс серии+размера из НАЗВАНИЯ (нужно для rusklimat: у него нет поля series,
  btu_calc битый). Паттерн «Бренд Серия МОДЕЛЬ-КОД» → серия + kBTU. Полезно для любых источников без серии.
- `ingest/__init__.py::collect_offers` — собрать `Offer` из БД + JAC.
- `pricing/pricing.py` — `compute_price(offer, cfg)` (опт×наценка→…90, `min_margin_abs` дефолт 0).
- `catalog/series.py` — `group_by_series`, `clean_series` (сливает близнецы: убирает хладагент R32/R410,
  год-версии, скобки; срезает префикс «…серии X»; инвертор/он-офф НЕ сливает).
- `models.py` — `Offer`, `RawProduct`, `City`, `Content`.

---

## 2. Фотоагент + очередь + АВТО-ПОБУДКА (ядро автоматизации — главный приз)
Карточки-картинки делает **веб-ChatGPT (GPT image 2)** браузерной автоматизацией на **локальном ПК владельца**
(Playwright+Chrome). Качество выше API. Проверено: ~30 карточек/день партиями, без бана.

**Очередь-API (на VPS, рядом с проектом ritualb2b):**
- FastAPI `vps_api` на `127.0.0.1:8765`. Репо фотоагента: `flycited2-dotcom/agent_convert_foto_rituailb2b2`
  (промпты `prompts/conditioner.txt`, `prompts/mcp.txt`, `prompts/kbt*`). Серверная часть/очередь —
  `/root/ritualb2b/` (`queue.db` SQLite, `output/` готовые картинки, `.env` с `API_TOKEN`).
- Связанный референс: `flycited2-dotcom/Splithub_api_telegram_me` (`stock_report_bot/`, `fotogen_bridge.py`) —
  как ставятся задачи и как бот шлёт результат в Telegram.

**Контракт submit-job** (`POST /api/submit-job`, заголовок `x-agent-token: <API_TOKEN>`):
форма `mode` (стиль промпта), `specs` (ТТХ-текст), `brand`, `model` (=серия), `chat_id` + файл `photo`
(входное фото товара). Ответ: `{"queued": "<input_filename>"}` — по нему маппим задачу.
Таблица `queue.db::jobs(status, input_filename, output_filename)`: `done` → картинка в `output/<output_filename>`.

**АВТО-ПОБУДКА (без ручного запуска агента):** запись `flags.agent_command='start'` в `queue.db` —
у владельца на ПК **WatchDog** (всегда запущен) ловит флаг и сам поднимает Chrome+агента (тот же механизм,
что кнопка «🚀 Запустить агента»). Т.е. ставим задачи + флаг → агент сам всё обработает.

**Мост (переиспользовать ЦЕЛИКОМ) — `cards_pipeline.py`:**
- `submit_card_job(cfg, photo_bytes, brand, model, specs, mode=…)` — POST в очередь.
- `done_results`/`failed_inputs(queue_db, input_filenames)` — читают `queue.db` (read-only).
- `wake_agent(queue_db)` — ставит `flags.agent_command='start'`.
- `CardJobStore` (`state/card_jobs.db`: key/input_filename/status/**tries**) — маппинг серия→задача.
- `run_once(groups, cfg, store)` — двухфазно: забрать готовые (`output/`→`cards/{key}.jpg`) + поставить
  новые (троттлинг `per_run`/`max_pending`/`max_total`) + **per-series режим** (`FotogenConfig.modes`) +
  **авто-ретрай failed** (до `MAX_TRIES=3`) + `wake_agent`.
- `cards_run.py` — CLI (env `FOTOGEN_*`, `FOTOGEN_MODES_JSON` = карта серия→режим).
- `content/cards.py` — `resolve_photos` (карточка вместо фото поставщика), `has_card`, `card_key`
  (имя файла = код товара после `:` в supplier_sku, кириллица сохраняется).

**Env фотоагента (на сервере `/opt/avito-bridge/.env`):**
`FOTOGEN_API_URL=http://127.0.0.1:8765`, `FOTOGEN_API_TOKEN` (из `/root/ritualb2b/.env` `API_TOKEN`),
`FOTOGEN_CHAT_ID=1264067528` (личка владельца, куда бот шлёт превью), `FOTOGEN_QUEUE_DB=/root/ritualb2b/queue.db`,
`FOTOGEN_OUTPUT_DIR=/root/ritualb2b/output`, `FOTOGEN_PER_RUN`, `FOTOGEN_MAX_PENDING`, `FOTOGEN_MAX_TOTAL`,
`FOTOGEN_MODE` (дефолт), `FOTOGEN_MODES_JSON`.

**Важно:** товар без входного фото поставщика карточку получить НЕ может (агенту нечего обрабатывать) —
такой товар держим в `held`/пропускаем (был кейс Ballu Tessey DC: 0 фото).

---

## 3. Генерация описаний (без LLM)
- `content/render.py` — продающий копирайт ВРУЧНУЮ (тип/BTU/площадь/выгоды/CTA, вариативность по артикулу,
  только правдивые данные), `_strip_stopwords`. Для avito — длинное; **для TG нужен КОРОТКИЙ вариант**
  (новый `render_caption`, ≤1024). ТТХ берутся из БД (`oasis_db.TECH_QUERY` по nc_code).
- `content/descriptions.py` + `avito-descriptions/manifest.json` — **переопределение описания на серию**
  готовым текстом (ручным/Codex): `manifest` сопоставляет `series_key`→файл; текст заменяет автогенерацию,
  живая цена/таблица дописывается. Этот же механизм переноси в Контент-завод (короткие тексты).
- `content/sizing.py` — типоразмер из btu_calc (карта площадей vs kBTU; выбор по монотонности цен).

---

## 4. Режимы карточек (стили)
`config/card_modes.json` = `{series_key: mode}`. Стили: `conditioner` (старый), `mcp` (премиум-glassmorphism,
нравится владельцу), `kbt`. В avito-каталоге использован сплит 13 mcp / 13 kbt / 11 conditioner. В Контент-заводе
режим задаётся **в задаче** (`mode=mcp`) и/или дефолтом в конфиге.

---

## 5. Инфраструктура, доступы, деплой
- VPS **213.109.202.45** (HestiaCP, Docker). SSH: `ssh -i ~/.ssh/climat_simf_deploy root@213.109.202.45`
  (ключ на машине владельца, root). Прод — сначала read-only; деструктив/внешнее — с подтверждения.
- Раздача статики: nginx `location /static/` → `/opt/oasis/staticfiles/` (карточки лежали
  `/opt/oasis/staticfiles/avito-cards/`, отдаются `https://splithome.ru/static/...`). nginx НЕ правили.
- systemd: `avito-cards.timer` (каждые ~2ч: ставит задачи в очередь + будит агента + забирает готовые),
  `avito-bridge.timer` (сборка/публикация). Аналоги нужны в Контент-заводе: «card worker» + «scheduler/publish».
- **Деплой** (scp на этом VPS НЕ работал): `tar -czf /tmp/x.tgz src config && ssh … 'cat > /tmp/x.tgz' < /tmp/x.tgz
  && ssh … 'cd /opt/<proj> && tar -xzf /tmp/x.tgz'`. Сборка/бэкап: `git push origin main` + `git push vps main`.
- GitHub `gh repo create` блокируется trade controls (Крым) — репо владелец создаёт вручную, push работает.

---

## 6. Грабли (проверено на практике)
- `.gitignore` в avito-bridge содержит широкие `*.txt`/`*.json` → новые тексты/манифесты НЕ коммитятся.
  Нужны негейты: `!avito-descriptions/`, `!…/*.txt`, `!config/card_modes.json`. Учесть в новом репо.
- **Шелл (Git Bash на Windows):** одинарные кавычки внутри SSH-команды в одинарных кавычках ломаются —
  серверные Python-скрипты слать через stdin: `ssh … '… .venv/bin/python -' < script.py`.
- Цепочка `cmdA && cmdB`: если у `cmdB` локальный редирект `< несуществующий_файл` — падает ДО `tar -xzf`,
  команда не выполняется (был такой баг с деплоем). Проверять наличие файла.
- `pytest | tail && commit` — `tail` маскирует код выхода → коммитился упавший тест. Использовать `pytest && commit`.
- Дубли в публикации: одинаковое фото у разных товаров = бан/дубль → решено уникальными карточками (фотоагент).
  Для TG-канала дубль-контроль — по state (не постить повторно один товар).
- btu_calc у rusklimat недостоверен (у всех размеров серии одно число) → размер парсить из модель-кода.
- Telegram: лимит подписи фото **1024 символа**; анти-flood (~20 msg/мин, на канал реже). Слать партиями.

---

## 7. Карта переиспользования кода
**Копировать/адаптировать (почти как есть):**
- `ingest/oasis_db.py`, `ingest/normalize.py`, `ingest/title_parse.py`, `ingest/__init__.py`
- `pricing/pricing.py`, `catalog/series.py`, `content/sizing.py`, `content/cards.py`,
  `content/descriptions.py`, `models.py`, `config.py` (адаптировать секции)
- **`cards_pipeline.py`** + `cards_run.py` (мост к фотоагенту — ядро, переносить целиком)

**Адаптировать сильнее:**
- `content/render.py` → добавить `render_caption` (короткий TG-вариант ≤1024)

**Заменить (Avito-специфика — НЕ нужна):**
- `feed/builder.py`, `feed/writer.py` (XML-фид) → `publish/telegram.py` (Bot API sendPhoto)
- `orchestrator/pipeline.py::run_cycle` (весь каталог) → `orchestrator/` (Task/Queue/Scheduler) +
  `review/rules.py` + `bot/commands.py` + `orchestrator/plans.py`

**Новое (нет аналога):**
- `bot/` (Telegram-бот: команды задач + превью/подтверждения-алерты), `publish/telegram.py`,
  `review/rules.py`, `orchestrator/tasks.py` + `queue.py` + `scheduler.py`, `orchestrator/plans.py` (YAML).

---

## 8. Состояние avito-bridge на момент передачи (2026-06-25)
Кондиционеры: **128 серий** после перегруппировки, **44 курируемые** опубликованы (выбор в
`Downloads/avito-series-select.xlsx`), 37/44 живы, остальное догенерялось карточками по таймеру.
100 тестов зелёные. Полная память проекта: `~/.claude/projects/.../memory/project_avito_bridge.md`
(см. `[[project_avito_bridge]]`). Это доказывает, что движок «контент→публикация» работает end-to-end —
Контент-завод меняет только «куда публикуем» (Avito → Telegram) и «как ставим задачи».
