import time

from content_factory.orchestrator.vk_content_plan import VkContentPlanStore, VkPlanCandidate


def test_failed_review_send_waits_before_retry(tmp_path):
    store = VkContentPlanStore(tmp_path / "plan.db")
    due = int(time.time()) + 3600
    item_id = store.add(VkPlanCandidate(
        source_key="catalog:x:1", source_ts=float(due), caption="Обогреватель X\n💎 1 000 ₽",
        card_path=str(tmp_path / "c.jpg"), category="heaters", brand="X"), due)
    now = int(time.time())
    assert not store.review_failed_recently(item_id, now)
    store.note_review_failure(item_id, "network: timeout")
    assert store.review_failed_recently(item_id, now)
    assert not store.review_failed_recently(item_id, now + 3601)
