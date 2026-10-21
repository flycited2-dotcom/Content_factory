"""Холодо- и теплопроизводительность кондиционера из ТТХ поставщика в БД сайта (catalog_producttech).

Владелец, 2026-10-21: в карточке и подписи кондиционера обязательно две строки после цены —
«❄️ Холод: 2,3 кВт», «🔥 Тепло: 2,5 кВт». Research фотоагента мощности не даёт, а БД сайта хранит
ТТХ поставщика: «Холодопроизводительность (кВт) = 2.30 (1.17 - 2.78)» — берём номинал (первое число).
Модель ищем по коду из наименования (RAC-I-AK25HP, RCI-RFS35HN). Не нашли — строк нет, не угадываем.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("content_factory")

COOL, HEAT = "Холодопроизводительность (кВт)", "Теплопроизводительность (кВт)"
_CODE = re.compile(r"[A-Z0-9][A-Z0-9.\-]*\d[A-Z0-9.\-]*")
_NUM = re.compile(r"\d+(?:[.,]\d+)?")

PRODUCT_QUERY = """
SELECT p.nc_code, p.title FROM catalog_product p
WHERE p.title ILIKE %(pattern)s
ORDER BY (p.title ILIKE '%%блок%%') , p.is_active DESC, p.id DESC
LIMIT 5
"""
SPEC_QUERY = """
SELECT ts.title, pt.value FROM catalog_producttech pt
JOIN catalog_techspec ts ON ts.id = pt.spec_id
JOIN catalog_product p ON p.id = pt.product_id
WHERE p.nc_code = %(nc)s AND ts.title IN %(titles)s
"""


def model_codes(name: str) -> list[str]:
    """Коды моделей из наименования, длинные первыми: «… AKOYA RAC-I-AK25HP.D01» → RAC-I-AK25HP.D01, …
    Части через «/» (внутренний/наружный блок) — отдельно."""
    words = [w for part in re.split(r"[\s/()]+", (name or "").upper()) for w in [part.strip(".,-")]]
    codes = [w for w in words if len(w) >= 5 and _CODE.fullmatch(w) and re.search(r"[A-Z]", w)]
    base = [c.split(".")[0] for c in codes if "." in c]          # RAC-I-AK25HP.D01 → RAC-I-AK25HP
    return sorted(dict.fromkeys(codes + base), key=len, reverse=True)


def nominal_kw(value: str) -> float | None:
    m = _NUM.search(value or "")
    return float(m.group(0).replace(",", ".")) if m else None


def _fmt(kw: float) -> str:
    return f"{kw:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def capacity_lines(cool: float | None, heat: float | None) -> str:
    lines = []
    if cool:
        lines.append(f"❄️ Холод: {_fmt(cool)} кВт")
    if heat:
        lines.append(f"🔥 Тепло: {_fmt(heat)} кВт")
    return "\n".join(lines)


class SiteSpecs:
    """Поиск мощности в БД сайта; connect() -> соединение psycopg2 (инъекция для тестов)."""

    def __init__(self, connect):
        self.connect = connect
        self._cache: dict[str, tuple] = {}

    def capacity(self, name: str) -> tuple[float | None, float | None]:
        if name in self._cache:
            return self._cache[name]
        found: tuple = (None, None)
        try:
            conn = self.connect()
            try:
                with conn.cursor() as cur:
                    for code in model_codes(name):
                        cur.execute(PRODUCT_QUERY, {"pattern": f"%{code}%"})
                        for nc, _title in cur.fetchall():
                            cur.execute(SPEC_QUERY, {"nc": nc, "titles": (COOL, HEAT)})
                            specs = dict(cur.fetchall())
                            if specs.get(COOL) or specs.get(HEAT):
                                found = (nominal_kw(specs.get(COOL, "")), nominal_kw(specs.get(HEAT, "")))
                                break
                        if found != (None, None):
                            break
            finally:
                conn.close()
        except Exception:                            # БД сайта недоступна — карточка без строк мощности
            log.warning("ТТХ мощности для «%s» не получены", name, exc_info=True)
            return (None, None)
        self._cache[name] = found
        return found

    def lines(self, name: str) -> str:
        return capacity_lines(*self.capacity(name))


def from_env() -> SiteSpecs:
    from decouple import config

    def connect():
        import psycopg2
        return psycopg2.connect(host=config("DB_HOST", "localhost"), port=config("DB_PORT", "5432"),
                                dbname=config("DB_NAME"), user=config("DB_USER"), password=config("DB_PASSWORD"),
                                connect_timeout=5)
    return SiteSpecs(connect)
