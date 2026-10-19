"""Снимает из плана темы, которые уже выходили на стену: это повторы."""
import sys

sys.path.insert(0, "/opt/content-factory-vk/src")
from content_factory.orchestrator.vk_content_plan import (  # noqa: E402
    PUBLISHED_EDITORIAL_STATUSES, VkContentPlanStore,
)

store = VkContentPlanStore("/opt/content-factory-vk/state/vk-plan.db")
items = store.list()
published = {i.source_key.split(":")[1] for i in items
             if i.source_key.startswith("editorial:") and i.status in PUBLISHED_EDITORIAL_STATUSES}
for item in items:
    if not item.source_key.startswith("editorial:") or item.status != "planned":
        continue
    topic = item.source_key.split(":")[1]
    if topic in published:
        ok = store._transition(item.id, ("planned",), "superseded")
        print(f"{item.id} {topic}: {'снят как повтор' if ok else 'не удалось'}")
