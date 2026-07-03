"""«Живой канал»: сверка опубликованных постов с каталогом. Чистая логика — сеть/state
снаружи (исполнитель — channel_sync_run). Правила: нет в наличии → sold («⛔ ПРОДАНО»
поверх сохранённой подписи); sold и снова в наличии → revive (свежая подпись);
цена ушла на ≥ min_price_delta → reprice. excel|* пропускаем (в прайсе нет остатков).
У записей без запомненной цены (первый прогон после миграции) — baseline: цену
запоминаем без правки поста."""
from __future__ import annotations
from dataclasses import dataclass

SOLD_MARK = "⛔ ПРОДАНО"


@dataclass
class SyncAction:
    key: str
    kind: str                 # sold | reprice | revive
    message_id: int
    channel: str
    caption: str              # полная новая подпись (≤1024 отрежет исполнитель)
    price: int | None = None  # новая цена для записи в state


def plan_sync(records, groups, price_fn, caption_fn, default_channel: str,
              min_price_delta: int = 100):
    """→ (actions, baseline): actions — правки постов; baseline — [(key, price)]
    для записи цены без правки (первая сверка после включения)."""
    by_key = {g.key: g for g in groups}
    actions, baseline = [], []
    for r in records:
        if r.key.startswith("excel|") or not r.message_id:
            continue
        chan = r.channel or default_channel
        g = by_key.get(r.key)
        in_stock = bool(g) and any((m.stock or 0) > 0 for m in g.members)
        if r.status != "sold" and not in_stock:
            cap = f"{SOLD_MARK}\n\n{r.caption}" if r.caption else SOLD_MARK
            actions.append(SyncAction(r.key, "sold", r.message_id, chan, cap))
            continue
        if not in_stock:
            continue                              # sold и по-прежнему нет — не трогаем
        price = price_fn(g)
        if r.status == "sold":
            actions.append(SyncAction(r.key, "revive", r.message_id, chan,
                                      caption_fn(g, price), price))
        elif price and r.price is None:
            baseline.append((r.key, price))       # первая сверка: запомнить без правки
        elif price and r.price and abs(price - r.price) >= min_price_delta:
            actions.append(SyncAction(r.key, "reprice", r.message_id, chan,
                                      caption_fn(g, price), price))
    return actions, baseline
