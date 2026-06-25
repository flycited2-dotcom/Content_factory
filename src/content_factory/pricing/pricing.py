from __future__ import annotations
from dataclasses import dataclass, field
from decimal import Decimal
from content_factory.models import Offer, PriceResult


def round_up_90(raw: float) -> int:
    """Округление ВВЕРХ до ближайшего числа, оканчивающегося на …90 (порт marked_price)."""
    base = (int(raw) // 100) * 100
    return base + 90 if raw <= base + 90 else base + 190


@dataclass
class PricingConfig:
    default_markup_pct: float = 5
    min_margin_abs: Decimal | int = 0   # 0 = без пола маржи (наценка строго +pct%); см. ТЗ §10
    rounding: str = "up_to_90"
    rules: list[dict] = field(default_factory=list)


def _markup_for(offer: Offer, cfg: PricingConfig) -> float:
    for rule in cfg.rules:
        m = rule.get("match", {})
        if all(getattr(offer, k, None) == v for k, v in m.items()):
            return float(rule["markup_pct"])
    return float(cfg.default_markup_pct)


def compute_price(offer: Offer, cfg: PricingConfig) -> PriceResult:
    if offer.cost is None or offer.cost <= 0:
        return PriceResult(ok=False, reason="cost<=0 or missing")
    pct = _markup_for(offer, cfg)
    cost = float(offer.cost)
    raw = cost * (1 + pct / 100.0)
    min_margin = float(cfg.min_margin_abs)
    min_applied = False
    if raw - cost < min_margin:
        raw = cost + min_margin
        min_applied = True
    price = round_up_90(raw)
    return PriceResult(ok=True, price=price, markup_pct=pct, min_margin_applied=min_applied)
