"""CLI автогенерации карточек: собрать серии → поставить задачи в очередь фотоагента и
забрать готовые карточки в cards/. Запускается по таймеру (throttle через FOTOGEN_PER_RUN).

  python -m content_factory.cards_run

В Контент-заводе выбор серий по задачам делает планировщик (orchestrator); этот CLI —
батч-генерация карточек по всему отфильтрованному каталогу (как card-worker в avito-bridge).
"""
from __future__ import annotations
import json
from pathlib import Path
from decouple import config
from content_factory.config import load_config
from content_factory.ingest import collect_offers
from content_factory.ingest.oasis_db import fetch_raw_products
from content_factory.catalog.series import group_by_series
from content_factory.cards_pipeline import FotogenConfig, CardJobStore, run_once
from content_factory.content.cards import build_modes_map
from content_factory.content.specs import build_specs_for_card
from content_factory.ingest.breez import fetch_breez_utp_by_nc, live_base_lookup


def main():
    cfg = load_config(Path("config/config.yaml"))
    dsn = {"host": config("DB_HOST", "localhost"), "port": config("DB_PORT", "5432"),
           "dbname": config("DB_NAME"), "user": config("DB_USER"), "password": config("DB_PASSWORD")}
    raw = fetch_raw_products(dsn, cfg.source.warehouse,
                             cfg.source.catalog.report_category_ids,
                             cfg.source.catalog.exclude_title_patterns)
    offers = collect_offers(raw, Path(config("JAC_STOCK_JSON", "")), cfg.source.catalog,
                            live_base_lookup())   # опт Бриза — как в планировщике/синке
    groups = group_by_series(offers)
    # Авто-выбор стиля карточки по категории товара (детерминированно, без ИИ): ручной
    # per-series override (card_modes.json) — высший приоритет, иначе карта cards.modes_by_category.
    overrides_path = Path(config("FOTOGEN_MODES_JSON", "config/card_modes.json"))
    overrides = json.loads(overrides_path.read_text(encoding="utf-8")) if overrides_path.exists() else {}
    default_mode = config("FOTOGEN_MODE", cfg.default_card_mode)
    modes, unknown = build_modes_map(groups, cfg.cards_modes_by_category, default_mode, overrides)
    unknown = sorted(c for c in unknown if c is not None)
    if unknown:                                    # не молчим: новая категория без mode → дефолт + предупреждение
        print(f"⚠️ нет mode для категорий {unknown} — берётся default '{default_mode}'; "
              f"добавьте их в cards.modes_by_category (config.yaml)")
    fcfg = FotogenConfig(
        api_url=config("FOTOGEN_API_URL", cfg.fotogen.api_url), token=config("FOTOGEN_API_TOKEN"),
        chat_id=int(config("FOTOGEN_CHAT_ID", "1264067528")),
        queue_db=config("FOTOGEN_QUEUE_DB"), output_dir=config("FOTOGEN_OUTPUT_DIR"),
        cards_dir=cfg.cards.dir, mode=default_mode, modes=modes,
        per_run=int(config("FOTOGEN_PER_RUN", str(cfg.fotogen.per_run))),
        max_pending=int(config("FOTOGEN_MAX_PENDING", str(cfg.fotogen.max_pending))),
        max_total=int(config("FOTOGEN_MAX_TOTAL", str(cfg.fotogen.max_total))))
    store = CardJobStore(Path(cfg.state.card_jobs_db))

    # Те же «ключевые особенности», что и в подписи → отдаём агенту на генерацию карточки.
    utp_map = fetch_breez_utp_by_nc()

    def specs_fn(g):
        rows = [{"title": t, "value": v} for m in g.members for t, v in (m.attrs or {}).items()]
        utp = utp_map.get(g.representative.supplier_sku.split(":", 1)[-1]) if g.source == "breeze" else None
        lines = build_specs_for_card(rows, g.brand, g.series, g.source, utp_raw=utp,
                                     titles=[m.model for m in g.members])
        return "\n".join(lines)

    submitted, published = run_once(groups, fcfg, store, specs_fn=specs_fn)
    print(f"cards: series={len(groups)} submitted={submitted} published={published}")


if __name__ == "__main__":
    main()
