from __future__ import annotations
import re
from pathlib import Path

import yaml

_AREA_TO_SIZE = {25: 7, 30: 9, 35: 12, 50: 18, 60: 24, 70: 24}
_SEMI_INDUSTRIAL_CAT = 6


def _apply_area_map(n: int | None, category_id: int | None) -> int | None:
    if n is None or category_id == _SEMI_INDUSTRIAL_CAT:
        return n
    return _AREA_TO_SIZE.get(n, n)


# ── power_map: ручной маппинг «код в номенклатуре поставщика → типоразмер» ──
# btu_calc в БД местами хаотичен (у одного кода встречаются 7/16/18), поэтому
# владелец размечает config/power_map.yaml: {source: {код: btu_true}}. Код —
# число, прилипшее к буквенному модельному коду (AS-07UW, RAC-SN20HP); btu_true
# 0/пусто = «код не мощность» (ревизия .D07, диаметр и т.п.) — игнорировать.

_REFRIG_CODE = re.compile(r"R\s?-?(?:32|410A?|290|22|134A)", re.I)
_MODEL_CODE = re.compile(r"[A-Z][A-Za-z]*[-_]?(\d{2})(?=[A-Z]|\b)")

_POWER_MAP: dict[str, dict[str, int]] = {}   # реестр процесса; ставится load_config


def power_codes(text: str) -> set[str]:
    """Кандидаты в коды мощности из модельного текста (хладагент вычищен).
    Диапазон 5..90 — отсекает ревизии (KB01) и не-мощностные числа."""
    found = _MODEL_CODE.findall(_REFRIG_CODE.sub("", text or ""))
    return {c for c in found if 5 <= int(c) <= 90}


def load_power_map(path: str | Path) -> dict[str, dict[str, int]]:
    """power_map.yaml → {source: {код: btu_true>0}}. Форма значения — dict с
    btu_true (как в размеченном черновике) или сразу число; пустые/0 — пропуск."""
    d = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    out: dict[str, dict[str, int]] = {}
    for source, codes in d.items():
        m: dict[str, int] = {}
        for code, v in (codes or {}).items():
            btu = v.get("btu_true") if isinstance(v, dict) else v
            if btu:
                m[str(code)] = int(btu)
        if m:
            out[source] = m
    return out


def set_power_map(pm: dict[str, dict[str, int]]) -> None:
    _POWER_MAP.clear()
    _POWER_MAP.update(pm or {})


def size_for(source: str, model_text: str, btu, category_id: int | None = None) -> int | None:
    """Типоразмер оффера: сперва ручной маппинг по коду номенклатуры, фолбэк —
    size_from_btu(btu_calc). Несколько кодов с РАЗНЫМИ мощностями → не гадаем, фолбэк."""
    m = _POWER_MAP.get(source) or {}
    sizes = {m[c] for c in power_codes(model_text) if m.get(c)}
    if len(sizes) == 1:
        return sizes.pop()
    return size_from_btu(btu, category_id)


def size_from_btu(btu, category_id: int | None = None, apply_area: bool = True) -> int | None:
    """Типоразмер (7/9/12/…) из btu_calc. См. ТЗ §11 и channel_caption.size_from_btu.
    apply_area=False — трактовать btu_calc как kBTU напрямую, без карты площадей
    (нужно, когда площадь-карта даёт неверный размер; выбор — на уровне серии по монотонности цен)."""
    try:
        v = float(btu)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    if v > 200:                 # полные BTU → kBTU
        v = v / 1000.0
    n = int(round(v))
    if not 1 <= n <= 200:
        return None
    return _apply_area_map(n, category_id) if apply_area else n
