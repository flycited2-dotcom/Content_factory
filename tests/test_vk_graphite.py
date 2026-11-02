from datetime import datetime, timedelta

from content_factory.orchestrator.vk_content_plan import VkContentPlanStore, VkPlanCandidate
from content_factory.orchestrator.vk_graphite import GraphiteQueue, is_graphite_slot, sync_graphite

CAPTION = ("Холодно на даче?\n\nОбогреватель Ballu BIGH-55\n💎 9 990 ₽ · в наличии\n\n"
           "✓ Защита от перегрева\n✓ Гарантия 12 мес")


class FakeQueue:
    def __init__(self):
        self.sent, self.answers = [], {}

    def queue(self):
        def submit(photo, brand, model, specs, mode):
            self.sent.append((brand, model, specs, mode))
            return f"in-{len(self.sent)}.jpg"
        return GraphiteQueue(submit=submit, results=lambda names: {
            n: self.answers[n] for n in names if n in self.answers})


def _setup(tmp_path, days_ahead=5):
    store = VkContentPlanStore(tmp_path / "plan.db")
    photo = tmp_path / "rusklimat_NS-1.jpg"
    photo.write_bytes(b"jpg")
    tile = tmp_path / "rusklimat_NS-1_tile.jpg"
    tile.write_bytes(b"tile")
    base = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=days_ahead)
    # Ищем графитовый слот: чередование по номеру слота.
    due = next(int((base + timedelta(minutes=30 * k)).timestamp()) for k in range(48)
               if is_graphite_slot(int((base + timedelta(minutes=30 * k)).timestamp())))
    item_id = store.add(VkPlanCandidate(
        source_key="catalog:rusklimat:NS-1", source_ts=float(due), caption=CAPTION,
        card_path=str(tile), category="heaters", brand="BALLU"), due)
    return store, item_id, tmp_path


def test_orders_graphite_card_for_future_graphite_slot(tmp_path):
    store, item_id, _ = _setup(tmp_path)
    fake = FakeQueue()
    result = sync_graphite(store, fake.queue(), int(datetime.now().timestamp()))
    assert result["submitted"] == [item_id]
    assert fake.sent == [("BALLU", "Обогреватель Ballu BIGH-55",
                          "Защита от перегрева\nГарантия 12 мес", "kbt")]
    # Повторный прогон не заказывает ещё раз.
    assert sync_graphite(store, fake.queue(), int(datetime.now().timestamp()))["submitted"] == []


def test_ready_card_replaces_tile_only_before_review(tmp_path):
    store, item_id, root = _setup(tmp_path)
    fake = FakeQueue()
    sync_graphite(store, fake.queue(), int(datetime.now().timestamp()))
    output = root / "agent_out.jpg"
    output.write_bytes(b"graphite")
    fake.answers["in-1.jpg"] = ("done", str(output))
    result = sync_graphite(store, fake.queue(), int(datetime.now().timestamp()))
    assert result["swapped"] == [item_id]
    card = store.get(item_id).card_path
    assert card.endswith("rusklimat_NS-1_graphite.jpg")
    assert open(card, "rb").read() == b"graphite"


def test_no_order_when_slot_is_too_close(tmp_path):
    store, _, _ = _setup(tmp_path, days_ahead=1)
    fake = FakeQueue()
    assert sync_graphite(store, fake.queue(), int(datetime.now().timestamp()))["submitted"] == []
