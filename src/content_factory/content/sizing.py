from __future__ import annotations

_AREA_TO_SIZE = {25: 7, 30: 9, 35: 12, 50: 18, 60: 24, 70: 24}
_SEMI_INDUSTRIAL_CAT = 6


def _apply_area_map(n: int | None, category_id: int | None) -> int | None:
    if n is None or category_id == _SEMI_INDUSTRIAL_CAT:
        return n
    return _AREA_TO_SIZE.get(n, n)


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
