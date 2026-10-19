"""Снимает из плана непоказанные и неотвеченные редакционные чек-листы старого формата."""
import sys

sys.path.insert(0, "/opt/content-factory-vk/src")
from content_factory.agents.editorial import load_ideas  # noqa: E402
from content_factory.orchestrator.vk_content_plan import VkContentPlanStore  # noqa: E402

ideas, _ = load_ideas("/opt/content-factory-vk/config/vk-editorial-sources.yaml")
old_topics = {idea.id for idea in ideas if idea.format == "checklist"}
store = VkContentPlanStore("/opt/content-factory-vk/state/vk-plan.db")

done = 0
for item in store.list():
    if not item.source_key.startswith("editorial:"):
        continue
    if item.source_key.split(":")[1] not in old_topics:
        continue
    if item.status in {"planned", "review", "visual_pending"}:
        if store._transition(item.id, ("planned", "review", "visual_pending"), "superseded"):
            done += 1
            print("снят:", item.id, item.source_key.split(":")[1], "(было:", item.status + ")")
print("снято:", done)
