"""Ежедневная сверка каталога SplitHub (таймер cf-splithub-sync):
products.json сайта → фильтр по наличию → снапшот prices_dir/splithub.json (слот
прайса: разделы-кнопки в /task) → УТП+фото в research_cache (research пропускается) →
отмена ещё не запущенных позиций, которых больше нет в наличии → сводка владельцу.

  python -m content_factory.splithub_sync
"""
from __future__ import annotations
from pathlib import Path

import httpx
from decouple import config

from content_factory.config import load_config
from content_factory.ingest import splithub_catalog as sh
from content_factory.ingest.excel_price import item_key
from content_factory.orchestrator.excel_pipeline import ExcelStore
from content_factory.publish.telegram import send_message

DEFAULT_STOCK_OK = ["in_stock"]
PLACEHOLDER = "placeholder.webp"


def sync(prices_dir, state_db, photo_dir, products, fetch_photo, stock_ok) -> dict:
    """Один прогон без сети (products/fetch_photo инъецируются). Сводка для лога/алерта."""
    store = ExcelStore(state_db)
    previous = {item_key(i) for i in sh.load_items(prices_dir)}
    candidates = [p for p in products
                  if p.get("group") in sh.GROUPS and sh.available(p, stock_ok)]
    photos = sh.prefetch_photos([p.get("photo") for p in candidates] + [PLACEHOLDER],
                                fetch_photo)
    bad = sh.bad_photo_names(photos, photos.pop(PLACEHOLDER, None))
    snap = sh.build_snapshot(products, stock_ok, bad_photos=bad)
    sh.write_snapshot(prices_dir, snap)
    seeded = sh.seed_cache(store, snap, photo_dir, photos)
    cancelled = sh.cancel_gone(store, snap, previous)
    by_section: dict[str, int] = {}
    for e in snap:
        by_section[e["section"]] = by_section.get(e["section"], 0) + 1
    return {"in_catalog": len(products), "in_stock": len(candidates),
            "listed": len(snap), "no_photo": len(candidates) - len(snap),
            "seeded": seeded, "cancelled": cancelled, "sections": by_section}


def format_summary(r: dict) -> str:
    lines = [f"🧊 SplitHub: в слот попало {r['listed']} из {r['in_catalog']} позиций "
             f"(в наличии {r['in_stock']}, без фото {r['no_photo']})."]
    lines += [f"• {name}: {n}" for name, n in sorted(r["sections"].items())]
    if r["cancelled"]:
        lines.append(f"Сняты с очереди (нет в наличии): {len(r['cancelled'])}")
    lines.append("Выбор: /task → категория.")
    return "\n".join(lines)


def main():
    cfg = load_config(Path("config/config.yaml"))
    sc = cfg.splithub or {}
    if not sc.get("enabled", True):
        print("splithub: disabled in config")
        return
    http = httpx.Client(timeout=60, follow_redirects=True)
    resp = http.get(sc.get("catalog_url", sh.CATALOG_URL))
    resp.raise_for_status()
    products = resp.json()
    if not isinstance(products, list) or not products:        # битый ответ ≠ «всё закончилось»
        raise SystemExit("splithub: каталог пуст/неверного формата — снапшот не трогаем")
    try:                                   # оверрайды админки сайта: цена/наличие/скрытие
        ov = http.get(sc.get("overrides_url", sh.OVERRIDES_URL))
        products = sh.apply_overrides(products, ov.json() if ov.status_code == 200 else None)
    except (httpx.HTTPError, ValueError):
        pass                               # нет оверрайдов — работаем по products.json
    base = sc.get("photo_base_url", sh.PHOTO_BASE_URL)

    def fetch_photo(name):
        try:
            r = http.get(base + name)
            return r.content if r.status_code == 200 and r.content else None
        except httpx.HTTPError:
            return None

    state_dir = Path(cfg.state.db).parent
    result = sync(state_dir / "prices", cfg.state.db, state_dir / "splithub" / "photos",
                  products, fetch_photo, sc.get("stock_ok", DEFAULT_STOCK_OK))
    print(f"splithub: {result}")
    token = config("TELEGRAM_BOT_TOKEN", "")
    owner = config("TELEGRAM_OWNER_CHAT_ID", config("FOTOGEN_CHAT_ID", ""))
    if token and owner and sc.get("notify", True):
        send_message(token, owner, format_summary(result), http=http)
    sync_splithome(cfg, state_dir / "prices")


def sync_splithome(cfg, prices_dir) -> None:
    """Весь ассортимент splithome.ru (вход +10%) — тем же суточным таймером; сбой не трогает splithub."""
    from content_factory.ingest import splithome_catalog
    from content_factory.ingest.breez import fetch_breez_base_by_nc
    dsn = {"host": config("DB_HOST", "localhost"), "port": config("DB_PORT", "5432"),
           "dbname": config("DB_NAME"), "user": config("DB_USER"), "password": config("DB_PASSWORD")}
    try:
        result = splithome_catalog.sync(prices_dir, dsn, cfg.source.warehouse, fetch_breez_base_by_nc())
        print(f"splithome: {result}")
    except Exception as exc:                     # noqa: BLE001 — суточный таймер не должен падать
        print(f"splithome: не обновлён — {exc.__class__.__name__}: {exc}")


if __name__ == "__main__":
    main()
