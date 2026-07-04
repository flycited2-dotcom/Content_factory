# Plan: Кнопочный флоу постановки задачи в Telegram-боте

Spec: [2026-07-04-bot-task-wizard-design.md](../specs/2026-07-04-bot-task-wizard-design.md)

## Ключевое решение по механике override (важно — не в спеке дословно)

Существующий `excel_pipeline.tick()` уже умеет пропускать research через
`ExcelStore.cache_get`/`cache_put(source='manual')`, НО пропуск срабатывает только
целиком (нужны И фото И УТП, иначе research всё равно перезатирает результат для
конкретного товара — проверено чтением кода). Трогать `tick()`/research-механизм —
явно вне скоупа (владелец: «механизм генерации уже всё хорошо»). Поэтому:

**v1-правило**: если владелец в мастере дал фото и/или УТП (хотя бы одно) — визард
СРАЗУ вызывает `submit_card(...)` напрямую (в обход research) и ставит товару
`status='card'`, минуя `new`/`research`. Если дано только фото — УТП в карточке будет
пустым (нет ✓-строк); если только УТП — фото берётся то, что дал владелец (пусто
недопустимо, тогда просим фото обязательно). Если НЕ дано ничего — обычный `new`,
как сейчас, отработает штатный тик. Это дешевле, чем частичный research, и не
трогает `excel_pipeline.py`.

`submit_card` сейчас — приватный closure внутри `excel_run.main()`, не переиспользуем
напрямую → шаг 2 выносит его в отдельный модуль.

| # | Goal | Touches | Done-check | Parallel? |
|---|---|---|---|---|
| 1 | **[РИСКОВАННЫЙ, ПЕРВЫМ]** Построчное сопоставление списка моделей с прайсом: `match_model_lines(items, lines, taken) -> list[LineMatch]` (per line: found item или None + топ-3 похожих кандидата). Прогнать на РЕАЛЬНЫХ строках из `price_test/БытТехОпт_20260702.xlsx` (не только на синтетике) — это проверка риска из спеки. | `src/content_factory/ingest/excel_price.py`, `tests/test_excel_price.py` | `pytest tests/test_excel_price.py -k match_model_lines` зелёный; ручной прогон на 15-20 реальных строках из БытТехОпт — match rate и список промахов показать владельцу до шага 5 | — |
| 2 | Вынести `submit_card`/`_slug`/`_silence` из `excel_run.main()` в переиспользуемый `make_card_submitter(api, headers, output_dir, owner_chat, queue_db)` → callable. `excel_run.py` использует новую фабрику вместо локального closure. | `src/content_factory/orchestrator/excel_run.py` (рефактор), новый `src/content_factory/orchestrator/card_submit.py`, `tests/test_card_submit.py` | `pytest tests/test_excel_pipeline.py tests/test_card_submit.py` зелёный; поведение `excel_run` не изменилось (те же вызовы API) | с #1 |
| 3 | `WizardStore` — состояние мастера на chat_id в SQLite (шаг, категория, накопленный список, photo_path, utp_text). Чистая логика без Telegram: `start()`, `set_category()`, `set_list()`, `set_photo()`, `set_utp()`, `cancel()`, `snapshot()`. Переживает рестарт процесса (требование из риск-регистра спеки). | новый `src/content_factory/bot/wizard.py`, `tests/test_wizard.py` | `pytest tests/test_wizard.py` зелёный (переходы между шагами, cancel сбрасывает, snapshot переживает новое подключение к той же БД) | с #1, #2 |
| 4 | `download_telegram_file(http, token, file_id) -> bytes` — общий хелпер получения файла (сейчас продублирован бы для фото; в `receive_price` уже есть похожий getFile-код — вынести и туда, и в новый путь). | `src/content_factory/bot/run.py` (рефактор `receive_price`), `tests/test_bot_run.py` | `pytest tests/test_bot_run.py` зелёный (старые тесты `receive_price`-подобные не ломаются, если есть; новый тест хелпера с MockTransport) | после #3 |
| 5 | Оркестрация мастера в `bot/run.py`: команда `/task` стартует флоу (первое сообщение — просьба назвать категорию текстом, без inline-кнопок на этом шаге); дальше — список моделей текстом (по строке); затем inline-кнопки «📎 Приложу фото»/«⏭ Пропустить»; затем «📝 Приложу УТП»/«⏭ Пропустить»; затем «✅ Подтвердить»/«❌ Отмена». На confirm: сопоставленные строки → `ExcelStore.add_items` + (если есть override) прямой `submit_card`+`status='card'` по правилу выше; несопоставленные — показать в ответе, не блокируя остальные. | `src/content_factory/bot/run.py`, `src/content_factory/bot/commands.py` (роутинг callback `wizard:*`), `tests/test_bot_run.py` | Тест «весь путь» (текст→текст→skip→skip→confirm) кладёт правильные ключи в `ExcelStore` со статусом `new`; отдельный тест override-пути (фото+УТП) кладёт `status='card'` с ожидаемым `card_job` (submit_card замокан) | после #2, #3, #4 |
| 6 | Кнопка «📊 Статус» — inline-алиас: callback `wizard:status` вызывает существующий `excel_fn()`, ответ тем же текстом, что `/excel`. Добавить `/task` и обновить `HELP` в `commands.py`. | `src/content_factory/bot/commands.py`, `src/content_factory/bot/run.py` | Тест: callback `wizard:status` возвращает то же, что `handle_command("/excel", ...)` при тех же данных | с #5 |
| 7 | Полный прогон тестов + деплой на VPS (бэкап файлов перед перезаписью, как обычно) + рестарт `cf-bot`. Один живой прогон в Telegram: `/task` → тестовая категория → 1-2 строки из реального прайса → skip/skip → confirm → проверить `/excel`, что позиции появились с ожидаемым статусом. | деплой (без новых файлов кода сверх уже описанных) | `pytest` весь набор зелёный локально ДО деплоя; после деплоя — реальный диалог в Telegram доходит до `/excel`-статуса без ошибок в `journalctl -u cf-bot` | — |

## Notes
- Каждый шаг ≤ ~1ч, заканчивается проверяемым состоянием (зелёные тесты/наблюдаемый результат).
- Шаг 1 — не просто написать функцию, а ПОКАЗАТЬ владельцу реальный match rate на его
  прайсе до того, как строить вокруг неё UI (шаг 5) — если рейт низкий, шаг 5
  переоценивается (может понадобиться нечёткий матч/топ-3 кандидата в UI, а не
  жёсткое «не найдено»).
- Существующие текстовые команды (`/make`, `/find`, `/pick`, `/excel`) не трогаются —
  весь новый код в НОВЫХ функциях/модулях + маршрутизация по префиксу `/task`/`wizard:*`.
- Если шаг 5 или 7 проваливает done-check дважды подряд — не третья попытка "ещё
  подправить", а возврат к спеке: скоуп override-механики (шаг «Ключевое решение»)
  пересматривается вместе с владельцем.
