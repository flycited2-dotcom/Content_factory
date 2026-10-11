# Хендофф 2026-10-11: ошибки конвейера, кнопка «повторить», выкладка

Для разработчика. Всё, что нужно, чтобы выложить правки и разобраться с ошибками. Секретов здесь нет.

## 1. Проблема
Владелец поставил в работу 15 позиций (Avito-конвейер Контент-завода). Нормально прошла **одна**,
остальные ушли в `failed`. В Telegram приходили сообщения:

- `research: Client error '403 Forbidden' for url 'https://www.vseinstrumenti.ru/product/...'`
- `research: research: source page does not contain exact model`

Страницы товаров в обычном браузере открываются, фото и характеристики на них есть. Раньше алерты шли без
причины и без возможности повторить; о повторной одинаковой ошибке бот молчал.

## 2. Репозитории и ветки (ничего не слито в master/main, PR нет)

| Репозиторий | Ветка | Коммиты | Что внутри |
|---|---|---|---|
| https://github.com/flycited2-dotcom/Content_factory | `claude/vigilant-fermi-hwrxac` (база `master`, `49a96fd`) | `aad60e4`, `722ee5e` | алерты с причиной и кнопкой повтора; `/excel` с причиной по-русски; этот документ |
| https://github.com/flycited2-dotcom/agent_convert_foto_rituailb2b2 | `claude/vigilant-fermi-hwrxac` (база `main`, `27f4e04`) | `1dc9130` | сравнение модели: кириллица в прайсе и латиница на сайте |

Если репозитории закрытые, владелец должен выдать разработчику доступ на GitHub.

## 3. Что изменено

**Content_factory** (4 файла в `src/content_factory/`):
- `orchestrator/excel_run.py`: `describe_failure()` переводит ошибку в причину и совет. `notify_new_failures()` шлёт
  **одно сообщение на позицию** с кнопкой «🔎 Искать заново» (`callback_data=exretry:<код>`, для сбоя карточки
  «🎨 Собрать карточку заново»), не больше 8 за запуск, неотправленные не помечаются.
- `orchestrator/excel_pipeline.py`: `ExcelStore.retry_item()` (повтор одной позиции) и `forget_failure_notices()`.
  `retry_failed()` (`/excel retry`) теперь тоже сбрасывает память «об ошибке уже сообщали».
- `bot/run.py`: `make_retry_excel_fn()`, обработчик callback `exretry:`, в `/excel` причина по-русски.
- `publish/telegram.py`: `send_message(..., reply_markup=None)`.

**Агент** (`agent.py`): `page_mentions_model` и `url_mentions_model` учитывают, что «КР-50-2БМ» из прайса и
`kr-50-2bm` в ссылке — одна модель (кириллическая буква → её латинские написания). Подтверждение остаётся
строгим: «КР-50-3БМ» не проходит, а длинное название вместо артикула сравнивается только буквально.
Причина в `download_verified_product_photo`: 403 допускался только если модель найдена в URL, а кириллицу с
латиницей сравнение не сопоставляло.

**Тесты:**
- Content_factory: `pytest -q` — 867 зелёных (Python 3.11+, зависимости из `pyproject.toml`: pydantic, httpx,
  PyYAML, python-decouple, Pillow, lxml, openpyxl, SpeechRecognition).
- Агент: новые тесты в `tests/test_parse_utp.py` (4 шт.). На Linux нужны заглушки модулей `win32clipboard`,
  `win32con`, `win32gui`, `win32api`. В полном прогоне агента есть падения, существовавшие до правок
  (нет `fastapi`; `asyncio.run() cannot be called from a running event loop`). Тест на 403 отдельно проходит.

## 4. ВАЖНО: сначала диагностика
Правки закрывают **не все** возможные причины. При 14 ошибках из 15 вероятно есть и другие. Не проверено:
- какая именно строка модели уходила агенту. Если в названии нет бренда, `ingest/excel_price.extract_model`
  отдаёт название целиком, и сравнение модели не поможет никогда;
- снимет ли фикс сам 403: сайты из среды разработки были недоступны.

**На сервере, только чтение, до выкладки:**

```bash
# 1) распределение ошибок
sqlite3 /opt/content-factory/state/content_factory.db "select substr(error,1,100) e, count(*) from excel_items where status='failed' group by e order by 2 desc;"
# 2) какая модель уходила агенту (путь к queue.db: FOTOGEN_QUEUE_DB в /opt/avito-bridge/.env; обычно /root/ritualb2b/queue.db)
sqlite3 /root/ritualb2b/queue.db "select id,brand,model,status,substr(error_text,1,110) from jobs order by id desc limit 30;"
```

Как читать:
- в `model` чистый артикул (`AFL50/260`, `КР-50-2БМ`) → правки агента должны помочь, после выкладки повторить позиции;
- в `model` название целиком → причина выше по потоку: `ready_price._model` / `ingest/excel_price.extract_model`;
- 403 остаётся при артикуле в ссылке → нужен запасной вариант: получать страницу через настоящий Chrome агента
  (Playwright/CDP). Не реализовано и не проверено;
- `no verified exact-model evidence` → ChatGPT не вернул JSON; смотреть дорожку, промпт `research`, `logs/agent.log`
  на машине агента;
- таймауты (`Timeout ...ms exceeded`) → проверить Chrome/ChatGPT на машине агента.

Автоповтор Avito-позиций уже есть: `ready_price._retry_delay` (30 минут для таймаутов/404/лимитов, иначе 6 часов).
`MAX_TRIES = 2` на этап (`orchestrator/excel_pipeline.py`).

## 5. Выкладка

### Сервер
VPS `212.116.115.150`, каталог `/opt/content-factory`. Старый `213.109.202.45` выключен и не трогается
(в CLAUDE.md репозитория агента этот адрес устарел). Доступ по SSH-ключу `id_ritualb2b_claude`, он у владельца.
`scp` на сервере не работает, только tar через ssh.

1. Проверка, что сервер не разошёлся с git:
```bash
cd /opt/content-factory && sha256sum src/content_factory/bot/run.py src/content_factory/orchestrator/excel_pipeline.py src/content_factory/orchestrator/excel_run.py src/content_factory/publish/telegram.py | cut -c1-16,65-
```
Ожидаются первые 16 символов: `b7262a528c8b54ff`, `55dd00a56b4e7c8f`, `bbfd1baabe5d6ed8`, `b737ff7452218f95`.
Иначе остановиться: на сервере есть правки вне git.

2. Бэкап и выкладка (с машины с git-клоном):
```bash
git checkout claude/vigilant-fermi-hwrxac && git pull
F="src/content_factory/bot/run.py src/content_factory/orchestrator/excel_pipeline.py src/content_factory/orchestrator/excel_run.py src/content_factory/publish/telegram.py"
tar -czf /tmp/cf.tgz $F
test -f /tmp/cf.tgz && ssh -i ~/.ssh/<ключ> root@212.116.115.150 'cat > /tmp/cf.tgz' < /tmp/cf.tgz
ssh -i ~/.ssh/<ключ> root@212.116.115.150 "cd /opt/content-factory && tar -czf bak-pre-retry-\$(date +%Y%m%d-%H%M).tgz $F && tar -xzf /tmp/cf.tgz && systemctl restart cf-bot.service && sleep 3 && systemctl is-active cf-bot.service && journalctl -u cf-bot -n 15 --no-pager"
```
Перезапускается только `cf-bot` (кнопку обрабатывает его процесс). `cf-excel.timer` срабатывает **каждую минуту**
(drop-in `deploy/cf-excel.timer.d/10-fast-poll.conf`) и подхватит новый код сам. `config/` и `deploy/` не
копировать, чтобы не затереть серверный `config.yaml`.

3. Откат:
```bash
cd /opt/content-factory && tar -xzf $(ls -t bak-pre-retry-*.tgz | head -1) && systemctl restart cf-bot.service
```

### Агент
Машина, где сейчас работает фотоагент (по CLAUDE.md репозитория агента от 30.06 — ноутбук; уточнить):
`git fetch && git checkout claude/vigilant-fermi-hwrxac`, затем **перезапустить `remote_agent.py`**: код читается
один раз при старте. Делать только когда нет задач в статусе `processing`, иначе задача зависнет на 30 минут.
Перезапуск: кнопка «🔁 Перезапуск агента» в боте или Планировщик (см. CLAUDE.md репозитория агента).

## 6. Проверка после выкладки
1. `/excel` в боте: в разделе «Ошибки» причина по-русски, без `Client error…`.
2. `/excel retry`: позиции вернутся в работу, упавшие снова придут **отдельными сообщениями с кнопкой**. Кнопка ставит
   одну позицию на повтор и будит конвейер. Старые сообщения в чате кнопок не получат.
3. Для Avito-позиций автоповтор через 30 минут или 6 часов остаётся; кнопка пропускает ожидание.

## 7. Что не сделано / не проверено
- Выкладка на сервер и на машину агента не выполнялась (у разработчика ИИ нет SSH-ключа).
- Не проверено на боевых данных и на живых сайтах.
- Запасной вариант получения страницы через Chrome агента (см. раздел 4) не реализован.
- Подмену User-Agent на браузерный не делали: эффект неизвестен, и это обход защиты сайта. Решает владелец.

## 8. Следующая задача: раздельные переключатели по направлениям
Владелец хочет независимые переключатели: **Avito**, **Telegram-канал**, **просто генерация карточек**, **ВК**.
Что есть в коде сейчас (проверено чтением кода, в бою не проверялось):

| Направление | Чем управляется сейчас | Замечания |
|---|---|---|
| Общая генерация (ручной конвейер `/task`, `/make`, `/pick`; карточки серий `cards_run`) | `/generation on\|off`, флаг `generation_enabled` в state-БД, по умолчанию **выключен** | `off` ещё и снимает ожидающий хвост из очереди фотоагента. Отдельного «только карточки» нет: карточки серий и ручной конвейер идут под одним флагом |
| Avito | `/avito pause\|resume\|start\|cancel\|restart`, флаг `ready_price_generation_enabled`, плюс файл `READY_PRICE_PUBLICATION_CONTROL` (остановка передачи новых карточек в фид) | Запуск партии требует **включённой общей генерации** (зависимость, а не независимый переключатель) |
| Telegram-канал (автопосты) | `/auto on\|off`, флаг `auto_enabled`; ручная публикация по ✅ под превью; `/sources` — выбор прайсов для Telegram | Отдельного выключателя «публикация в канал» нет: автопосты идут через `/auto` и `confirm` |
| ВК | план публикаций: `/vkplan`, `/vkpost`, `/vkrevision`, кнопки ревью | Отдельного вкл/выкл в просмотренном коде не нашли (есть лишь env `VK_NATIVE_PHOTO_ENABLED`, `VK_EDITORIAL_SERVICE_CTA_ENABLED` для подфункций) |

Нужно решить с владельцем: единая панель «Переключатели» в боте с четырьмя независимыми выключателями (Avito,
Telegram-канал, генерация карточек, ВК); что должен останавливать каждый (генерацию, публикацию или оба);
значения по умолчанию после деплоя (сейчас безопасный вариант — всё выключено); куда идут алерты об ошибках по
каждому направлению (сейчас они приходят из нескольких мест: конвейер прайса, планировщик, ВК-цикл, синхронизация цен).

## 9. Правила
- Секреты (`.env`, токены, креды БД) не в git и не в чат.
- Публикация в боевой Telegram-канал только с явного ОК владельца; тесты слать в личку.
- В `main` и `master` без согласия владельца не вливать.
