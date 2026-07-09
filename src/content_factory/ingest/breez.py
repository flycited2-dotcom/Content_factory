"""Breez API: то, чего нет в БД сайта. Порт из Splithub stock_report_bot/breez.py
(httpx вместо requests; креды из .env). Сайт oasis НЕ задействован.

1. УТП (`utp`): готовый список преимуществ по nc_code. Синк сайта кладёт в БД
   только tech-характеристики, поле `utp` из `/products/` теряется.
2. Опт-цена (`base`): Бриз отдаёт опт/закупку только в `/leftoversnew/`; в БД
   сайта у Бриза лежит РОЗНИЦА. Rusklimat/Daichi опт берут из БД (там он есть)."""
from __future__ import annotations
import logging
from decimal import Decimal
from typing import Callable
import httpx
from decouple import config

log = logging.getLogger("content_factory")


def _parse_products_utp(data) -> dict:
    """Из ответа `/products/` (dict id→продукт) → {nc_code: utp_raw}. Чистая функция."""
    result = {}
    if not isinstance(data, dict):
        return result
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        nc = entry.get("nc")
        utp = entry.get("utp")
        if nc and utp and str(utp).strip():
            result[str(nc)] = str(utp)
    return result


def fetch_breez_utp_by_nc(base_url: str | None = None, auth_header: str | None = None,
                          http: httpx.Client | None = None) -> dict:
    """{nc_code: utp_raw} из Breez `/products/`. Пусто, если ключ/URL не заданы или
    запрос упал → блок особенностей обойдётся без ✓-УТП (структурные пункты из БД)."""
    base_url = base_url if base_url is not None else config("BREEZ_BASE_URL", "")
    auth_header = auth_header if auth_header is not None else config("BREEZ_AUTH_HEADER", "")
    if not base_url or not auth_header or "REPLACE" in auth_header:
        log.warning("breez utp: ключ/URL не заданы — УТП Бриза недоступно")
        return {}
    url = base_url.rstrip("/") + "/products/"
    try:
        client = http or httpx.Client(timeout=120, trust_env=False)
        r = client.get(url, headers={"Authorization": auth_header, "Accept": "application/json"})
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # noqa: BLE001
        log.error("breez utp fetch failed: %s", e)
        return {}
    res = _parse_products_utp(data)
    log.info("breez: utp по %d позициям", len(res))
    return res


def _extract_base(price):
    """Из `price: [{base, base_currency}, {ric, ric_currency}]` достаём base (опт)."""
    if isinstance(price, list):
        for p in price:
            if isinstance(p, dict) and p.get("base") is not None:
                return p["base"]
    return None


def _parse_leftovers(data) -> dict:
    """Из ответа `/leftoversnew/` → {nc_code: base}. Чистая функция.

    Форматы (как `_iter_leftoversnew` у сайта):
    - Format 1: `{"НС": {...запись...}}` — ключ = NC (в записи поля `nc` может
      не быть) — текущий живой формат;
    - Format 2: `[{"НС": {...запись...}}]` — список одноключевых dict;
    - плоский: `[{"nc"/"nc_code"/"id": ..., "price": ...}]`.
    """
    if isinstance(data, dict):
        entries = [(key, val) for key, val in data.items() if isinstance(val, dict)]
    elif isinstance(data, list):
        entries = []
        for e in data:
            if not isinstance(e, dict):
                continue
            if len(e) == 1 and isinstance(next(iter(e.values())), dict):
                entries.append(next(iter(e.items())))   # (NC, запись) — Format 2
            else:
                entries.append((None, e))                # плоский — nc внутри записи
    else:
        return {}

    result = {}
    for key, entry in entries:
        nc = entry.get("nc") or entry.get("nc_code") or entry.get("id") or key
        base = _extract_base(entry.get("price"))
        if nc and base is not None:
            result[str(nc)] = base
    return result


def fetch_breez_base_by_nc(base_url: str | None = None, auth_header: str | None = None,
                           http: httpx.Client | None = None) -> dict:
    """{nc_code: base_price} из Breez `/leftoversnew/`. Пусто, если ключ/URL не заданы
    или запрос упал → потребитель мягко откатывается на цену из БД (розница Бриза)."""
    base_url = base_url if base_url is not None else config("BREEZ_BASE_URL", "")
    auth_header = auth_header if auth_header is not None else config("BREEZ_AUTH_HEADER", "")
    if not base_url or not auth_header or "REPLACE" in auth_header:
        log.warning("breez base: ключ/URL не заданы — опт Бриза будет из БД (розница)")
        return {}
    url = base_url.rstrip("/") + "/leftoversnew/"
    try:
        client = http or httpx.Client(timeout=60, trust_env=False)
        r = client.get(url, headers={"Authorization": auth_header, "Accept": "application/json"})
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # noqa: BLE001
        log.error("breez base fetch failed: %s", e)
        return {}
    res = _parse_leftovers(data)
    log.info("breez: опт-цен (base) по %d позициям", len(res))
    return res


def base_lookup(base_map: dict) -> Callable[[str | None], Decimal | None]:
    """Мост к collect_offers: {nc: число из JSON} → лукап nc_code → Decimal | None
    (resolve_cost ждёт Decimal; None → мягкий фолбэк на цену из БД — розницу)."""
    def lookup(nc):
        v = base_map.get(str(nc)) if nc else None
        return Decimal(str(v)) if v is not None else None
    return lookup


def live_base_lookup(base_url: str | None = None, auth_header: str | None = None,
                     http: httpx.Client | None = None) -> Callable[[str | None], Decimal | None]:
    """Свежий фетч + лукап одной строкой — общий для раннеров (scheduler_run,
    cards_run, channel_sync_run): все конвейеры должны считать Бриз от одного опта,
    иначе синк перезапишет цены планировщика розницей."""
    return base_lookup(fetch_breez_base_by_nc(base_url=base_url, auth_header=auth_header,
                                              http=http))
