"""Breez API: УТП (готовый список преимуществ) по nc_code — того, чего нет в БД сайта.
Синк сайта кладёт в БД только tech-характеристики, поле `utp` из `/products/` теряется.
Порт из Splithub stock_report_bot/breez.py (httpx вместо requests; креды из .env).
Сайт oasis НЕ задействован."""
from __future__ import annotations
import logging
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
