# Синхронизация с сервером — 2026-10

Источник: read-only снимок /opt/content-factory от 2026-10-19 (отчёт: Agents_MY/docs/reports/2026-10-19-server-vs-repos.md).
Правило: серверный файл берётся, если изменён позже последнего коммита этого файла в репозитории.
Исключение: `src/content_factory/bot/wizard_flow.py` взят с сервера, хотя коммит на 2 минуты новее, — серверная версия
содержит всё из репозитория плюс 88 строк меню SplitHub.
Не переносились: 64 резервные копии с сервера (`*.bak*`, `*.before-*`, `.aru-code-deploy.lock`).

## Взято с сервера (только там было)
- `config/stabilizers-b2c.yaml`
- `config/vk-editorial-sources.yaml`
- `deploy/cf-splithub-sync.service`
- `deploy/cf-splithub-sync.timer`
- `deploy/cf-vk-plan.service`
- `deploy/cf-vk-plan.timer`
- `deploy/cf-vk-text.service`
- `deploy/cf-vk-text.timer`
- `docs/HANDOFF-2026-09-22-CLAUDE-TO-CODEX.md`
- `docs/NIKITA-LOGIC.md`
- `scripts/drop_repeats.py`
- `scripts/install_editorial_visuals.py`
- `scripts/live_run_approve.py`
- `scripts/live_run_time.py`
- `scripts/refresh_captions.py`
- `scripts/refresh_catalog_posts.py`
- `scripts/relink_editorial_cards.py`
- `scripts/render_text_cards.py`
- `scripts/retire_old_cards.py`
- `scripts/submit_missing_visuals.py`
- `scripts/vk_attach_editorial_visual.py`
- `scripts/vk_refresh_editorial_posts.py`
- `src/content_factory/archive_reuse.py`
- `src/content_factory/ingest/splithub_catalog.py`
- `src/content_factory/splithub_sync.py`
- `src/content_factory/storefront/site_dump_script.txt`

## Взято с сервера (новее)
- `config/config.yaml`
- `deploy/update_db_host.sh`
- `requirements.txt`
- `src/content_factory/bot/run.py`
- `src/content_factory/config.py`
- `src/content_factory/ingest/aru_details.py`
- `src/content_factory/ingest/excel_price.py`
- `src/content_factory/orchestrator/excel_pipeline.py`
- `src/content_factory/orchestrator/excel_run.py`
- `src/content_factory/publish/telegram.py`
- `src/content_factory/publish/vk.py`
- `src/content_factory/bot/wizard_flow.py`

## Оставлено из репозитория (в репозитории новее сервера — ещё не задеплоено)
- `docs/BOT-MENU.md`
- `docs/SYSTEM-RECOVERY.md`
- `pyproject.toml`
- `src/content_factory/bot/control_menu.py`

`control_menu.py` отличается от прода только порядком кнопок (намеренное отличие, см. REPOSITORY-SYNC-2026-10-05.md).

## Юниты systemd
С сервера добавлены: cf-vk-analytics, cf-vk-capabilities, cf-vk-storefront, cf-vk-weekly-report (.service/.timer),
drop-in'ы cf-vk-plan.service.d/live.conf и cf-vk-plan.timer.d/interval.conf; cf-vk-plan, cf-vk-text, cf-splithub-sync — из /opt/content-factory/deploy.

## Тесты
До: 857 passed. После: 848 passed, 9 failed.

### Тесты, расходящиеся с продом (код прода новее тестов, тесты не правились)
- 7 тестов (`test_excel_cache_recovery.py`, `test_excel_cancel_reselect.py`, `test_bot_run.py::test_excel_fn_truncates_multiline_errors_and_separates_sections` — частично):
  конвейер прода возвращает новые счётчики `reused_card`, `reused_photo` (повторное использование карточек из архива, `archive_reuse.py`).
- `test_excel_pipeline.py::test_failure_notification_deduplicates_retry_and_excludes_old_items`: прод передаёт `reply_markup` в уведомление об ошибке.
- `test_bot_run.py::test_excel_fn_truncates_multiline_errors_and_separates_sections`: изменён формат обрезки текста ошибки.
Решение (обновить тесты под прод) — за владельцем.

## Открытый вопрос: вторая копия кода в проде
`/opt/content-factory-vk` — отдельное развёртывание (по составу совпадает с репозиторием Codex/1st_content_TG_VK_YT_INSTA_autoload,
21 файл отличается от него; с /opt/content-factory различаются 20 общих файлов). Обслуживает ВК-планер. В этот репозиторий не сливалось —
ждёт решения владельца, какая версия ВК-модулей правильная.
