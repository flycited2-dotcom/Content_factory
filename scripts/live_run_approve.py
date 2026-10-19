"""Готовит показательную тройку постов к боевому прогону.

Владелец попросил живой прогон, чтобы увидеть, как записи выглядят в группе.
Берём по одному материалу каждого типа: информационный, сервисный и товарный.
"""
import sys
import time

sys.path.insert(0, "/opt/content-factory-vk/src")
from content_factory.orchestrator.vk_content_plan import VkContentPlanStore  # noqa: E402

STORE = VkContentPlanStore("/opt/content-factory-vk/state/vk-plan.db")
IDS = (39, 38, 1)
due_at = int(time.time()) + 120

for item_id in IDS:
    item = STORE.get(item_id)
    if item is None:
        print(f"{item_id}: записи нет")
        continue
    if item.status == "planned":
        STORE.mark_review(item_id, None)
    if not STORE.approve(item_id):
        print(f"{item_id}: одобрить не удалось, статус {STORE.get(item_id).status}")
        continue
    # Время публикации переносим на ближайшие минуты: иначе материал ждёт
    # своего слота и прогон пришлось бы откладывать на сутки.
    with STORE._connect() as connection:
        connection.execute(
            "UPDATE vk_content_plan SET due_at=?,updated_at=? WHERE id=? AND status='approved'",
            (due_at, int(time.time()), item_id),
        )
    fresh = STORE.get(item_id)
    print(f"{item_id}: {fresh.status}, публикация в "
          f"{time.strftime('%H:%M:%S', time.localtime(fresh.due_at))} — {fresh.content_type}")
