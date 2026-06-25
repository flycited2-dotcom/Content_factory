from __future__ import annotations
from decimal import Decimal
from content_factory.models import RawProduct


def resolve_cost(raw: RawProduct, breez_base: Decimal | None) -> Decimal | None:
    """Опт (себестоимость) по поставщику. См. ТЗ §5.2."""
    src = (raw.source or "").lower()
    if src == "breeze":
        return breez_base if breez_base is not None else raw.price_wholesale
    if src == "rusklimat":
        return raw.price_base if raw.price_base is not None else raw.price_wholesale
    # daichi, jac и прочие: «Ваша цена»/опт лежит в price_wholesale
    return raw.price_wholesale
