"""Наценки БД-источников из бота (/markup breeze -3, /markup * 8) — поверх yaml.
Хранение — таблица settings state-БД (та же, что флаг /auto), ключи `markup:<source>`;
'*' меняет дефолт. Excel-прайсы НЕ здесь: у них своя наценка per-файл (set_markup).
Применение: раннеры оборачивают cfg.pricing → apply_overrides(...) перед compute_price."""
from __future__ import annotations
import sqlite3
from pathlib import Path
from content_factory.pricing.pricing import PricingConfig

_PREFIX = "markup:"


def _c(db) -> sqlite3.Connection:
    p = Path(db)
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    return c


def markup_overrides(db) -> dict[str, float]:
    """{source: pct} из state-БД ('*' = дефолт для всех без спец-правила)."""
    with _c(db) as c:
        rows = c.execute("SELECT key, value FROM settings WHERE key LIKE ?",
                         (_PREFIX + "%",)).fetchall()
    return {k[len(_PREFIX):]: float(v) for k, v in rows}


def set_markup_override(db, source: str, pct: float | None) -> None:
    """pct=None — убрать override (вернуться к yaml)."""
    with _c(db) as c:
        if pct is None:
            c.execute("DELETE FROM settings WHERE key=?", (_PREFIX + source,))
        else:
            c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                      (_PREFIX + source, str(float(pct))))


def apply_overrides(cfg: PricingConfig, overrides: dict[str, float]) -> PricingConfig:
    """Новый PricingConfig: per-source overrides встают ПЕРЕД yaml-правилами
    (приоритетнее), '*' заменяет default_markup_pct. Пустые overrides → cfg как есть."""
    if not overrides:
        return cfg
    rules = [{"match": {"source": s}, "markup_pct": p}
             for s, p in overrides.items() if s != "*"]
    return PricingConfig(default_markup_pct=overrides.get("*", cfg.default_markup_pct),
                         min_margin_abs=cfg.min_margin_abs, rounding=cfg.rounding,
                         rules=rules + cfg.rules)
