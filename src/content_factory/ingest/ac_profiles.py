"""Профиль кондиционера для отбора по словам владельца: мороз на обогрев, инвертор, тепловой насос, Full DC.

Владелец, 2026-10-21: «инверторы от −15 вплоть до −35, тепловые насосы, Full DC / 3DC — нужен фильтр».
Данные — ТТХ поставщиков в БД сайта splithome.ru, у каждого своя запись:
  Бриз      «Рабочие температурные границы наружного воздуха (нагрев)» = «-15 ~ +24», «Тепловой насос» = да/нет
  Русклимат «Мин. рабочая температура воздуха для внешнего блока» = «-20»
  Daichi    «Диапазон рабочих температур, нагрев, °C» = «-20~24»
  все       «Инверторная технология» = да/нет
Full DC и «тепловой насос» встречаются ещё в названии/УТП. Профили строит суточная синхронизация
(prices_dir/ac_profiles.json, ключ — sku позиции слота); отбор читает файл, БД в момент выбора не нужна.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from content_factory.ingest.site_specs import model_codes

PROFILES_NAME = "ac_profiles.json"
FROST_SPECS = ("Рабочие температурные границы наружного воздуха (нагрев)",
               "Мин. рабочая температура воздуха для внешнего блока",
               "Диапазон рабочих температур, нагрев, °C")
INVERTER_SPEC, HEAT_PUMP_SPEC = "Инверторная технология", "Тепловой насос"
QUERY = """
SELECT p.nc_code, p.title, ts.title AS spec, pt.value
FROM catalog_producttech pt
JOIN catalog_techspec ts ON ts.id = pt.spec_id
JOIN catalog_product p ON p.id = pt.product_id
WHERE ts.title IN %(specs)s
"""
_SIGNED = re.compile(r"([-−–]?)\s*(\d+(?:[.,]\d+)?)")


def frost_from_spec(value: str) -> int | None:
    """Первая граница диапазона: «-15 ~ +24» → 15, «-20» → 20; положительная — не про мороз (ошибка данных)."""
    m = _SIGNED.search(value or "")
    if not m:
        return None
    number = float(m.group(2).replace(",", "."))
    return int(number) if m.group(1) else (0 if number == 0 else None)


def yes(value: str) -> bool | None:
    v = (value or "").strip().lower()
    return True if v in ("да", "yes", "есть") else False if v in ("нет", "no") else None


def text_flags(text: str) -> dict:
    low = (text or "").lower()
    return {"full_dc": "full dc" in low or "full-dc" in low,
            "heat_pump_text": "тепловой насос" in low or "heat pump" in low,
            "inverter_text": "инвертор" in low or "inverter" in low}


def build_profiles(rows: list[tuple], items: list[dict]) -> dict[str, dict]:
    """rows — (nc, title, spec, value) из БД; items — позиции слотов: {sku, name, utp?, group?}."""
    specs: dict[str, dict] = {}
    titles: dict[str, str] = {}
    for nc, title, spec, value in rows:
        specs.setdefault(nc, {})[spec] = value
        titles[nc] = title or ""
    by_code: dict[str, str] = {}
    for nc, title in titles.items():
        for code in model_codes(title):
            by_code.setdefault(code, nc)
    out = {}
    for item in items:
        nc = next((by_code[c] for c in model_codes(item.get("name", "")) if c in by_code), None)
        s = specs.get(nc, {})
        frost = next((f for t in FROST_SPECS if (f := frost_from_spec(s.get(t, ""))) is not None), None)
        flags = text_flags(f"{item.get('name', '')}\n{item.get('utp', '')}")
        inverter = yes(s.get(INVERTER_SPEC, ""))
        if inverter is None and item.get("group") in ("inv", "pac_inv"):
            inverter = True
        elif inverter is None and item.get("group") in ("onoff", "pac_onoff"):
            inverter = False
        out[item["sku"]] = {"min_temp": frost,
                            "inverter": inverter if inverter is not None else (flags["inverter_text"] or None),
                            "heat_pump": bool(yes(s.get(HEAT_PUMP_SPEC, "")) or flags["heat_pump_text"]),
                            "full_dc": flags["full_dc"]}
    return out


def write_profiles(prices_dir, profiles: dict) -> None:
    path = Path(prices_dir) / PROFILES_NAME
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(profiles, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def read_profiles(prices_dir) -> dict:
    path = Path(prices_dir) / PROFILES_NAME
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def sync(prices_dir, dsn: dict) -> int:
    """Профили для кондиционеров слотов splithub.ru и splithome.ru."""
    import psycopg2
    from content_factory.ingest import splithome_catalog, splithub_catalog
    items = [e for e in splithub_catalog.read_snapshot(prices_dir) if e.get("group") in
             ("inv", "onoff", "pac_inv", "pac_onoff")]
    items += [e for e in splithome_catalog.read_snapshot(prices_dir)
              if "сплит" in (e.get("section") or "").lower() or "кондиционер" in (e.get("section") or "").lower()]
    conn = psycopg2.connect(connect_timeout=10, **dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(QUERY, {"specs": (*FROST_SPECS, INVERTER_SPEC, HEAT_PUMP_SPEC)})
            rows = cur.fetchall()
    finally:
        conn.close()
    profiles = build_profiles(rows, items)
    write_profiles(prices_dir, profiles)
    return len(profiles)
