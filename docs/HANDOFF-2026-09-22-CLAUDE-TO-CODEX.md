# Claude → Codex, 2026-09-22: что я поменял на проде и как не мешать друг другу

## Коротко
Владелец жаловался: «Никита молчит/тормозит», пропали категории в /task, в ручную
генерацию лезут огромные прайсы. Всё исправлено **точечными правками прямо на проде**
`/opt/content-factory` (бэкапы рядом). Твою зону (Avito-автоконвейер, VK, витрина) не
трогал — контракт `load_price_slots()` по умолчанию прежний.

## Правки на проде (22.09, все с бэкапами)
| Файл | Что | Бэкап |
|---|---|---|
| `bot/run.py` | httpx клиента бота: `Timeout(40, connect=4)` + `HTTPTransport(retries=3)` (обрывы связи с Telegram ~30%) | `.bak-20260922-tgretry` |
| `bot/run.py` | `/sources`: номера + кнопки 🟢/⚪ (`srctg:N`), `sources_markup()`, `toggle_tg_source()`; /make и /find → `for_telegram=True` | `.bak-20260922-tgsources` |
| `bot/wizard_flow.py` | `_price_items()` → `load_price_slots(..., for_telegram=True)` | `.bak-20260922-tgsources` |
| `ingest/excel_price.py` | `_borrow_sections()` (у БытТехОпт с правки 24.08 пропали разделы: 130 категорий) + фильтр разделов >70 символов | `.bak-20260922-sections` |
| `ingest/excel_price.py` | `tg_disabled()/set_tg_enabled()` (`state/prices/telegram_sources.json`), `load_price_slots(dir, for_telegram=False)`, кэш разбора `_parse_cached()` по mtime/size (копии позиций) | `.bak-20260922-tgsources`, `.bak-20260922-speed` |

Данные: для Telegram выключены `mail__b2bportal_brinex_ru_прайсбринэкс` и
`mail__ma_kobzeva_instrument_ru_прайс_на_21_09_26` (Avito их по-прежнему видит).

**Прошу:** перенеси эти правки в свою ветку `codex/telegram-avito-integration`,
прежде чем снова деплоить эти три файла, — иначе они откатятся. Полный текст файлов
как на проде — в `Content_factory`, ветка `server-snapshot-2026-09-22` (коммиты
253a06c, b548c78, 517c6cd). Логика бота — `docs/NIKITA-LOGIC.md`.

## Разделение зон (предлагаю)
- **Claude:** ручная генерация «Никиты» — `bot/run.py` (команды/кнопки Telegram),
  `bot/wizard_flow.py`, `bot/commands.py`, Telegram-фильтр в `ingest/excel_price.py`.
- **Codex:** Avito-автоконвейер и всё рядом — `ready_price*.py`,
  `private_price_catalog.py`, `ingest/avito_*`, `storefront/*`, `publish/vk*`,
  `analytics/*`, `orchestrator/vk_content_plan.py`.
- **Общие файлы** (`bot/run.py`, `ingest/excel_price.py`, `orchestrator/excel_*`):
  перед правкой — запись в `.codex-claude/state/OWNERSHIP.md` и сверка с продом
  (`diff` прод ↔ твоя копия). Никогда `tar src` поверх прода целиком.

## Что ещё увидел (к сведению)
1. Прод расходится с обоими git-репо. `Content_factory/master` отстал на ~2 месяца;
   твоя копия совпадает с продом по 66 из 86 модулей. Снимки: `server-snapshot-2026-09-22`
   (прод) и `desktop-wip-2026-09-22` (незакоммиченное с десктопа) в `Content_factory`.
2. На десктопе в `content-factory` лежит незакоммиченный WIP: `control_menu.py`
   (меню + рубильник генерации), `ready_price*`. `control_menu.py` есть на проде,
   но прод-`run.py` его НЕ импортирует — меню, вероятно, не работает. Твоё?
3. Репо агента (`agent_convert_foto_rituailb2b2`): WIP десктопа влит в `main`
   (e2e987a = прод vps_api/vps_bot). Кто-то сейчас правит там `lane-control-state`
   (watchdog/vps_api/vps_bot, не закоммичено). В watchdog `raise_for_status()` на
   `/api/lane-control-state` — если запустить до деплоя эндпоинта на VPS, вотчдог уйдёт
   в цикл переподключений туннеля.
4. Упал `cf-vk-analytics.service` (22.09 21:34, exit-code).
5. `excel_items`: 468 позиций в `new` ждут генерации.
