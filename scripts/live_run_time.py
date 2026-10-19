"""Сдвигает время боевого прогона за ближайший запуск таймера.

Публикатор берёт только записи, у которых срок ещё впереди: прошедшее время
он не публикует, а переносит в свободный слот. Поэтому назначаем публикацию
заведомо позже очередного запуска cf-vk-plan.timer.
"""
import sys
import time

sys.path.insert(0, "/opt/content-factory-vk/src")
from content_factory.orchestrator.vk_content_plan import VkContentPlanStore  # noqa: E402

STORE = VkContentPlanStore("/opt/content-factory-vk/state/vk-plan.db")
due_at = int(time.time()) + 21 * 60

for item_id in (39, 38, 1):
    with STORE._connect() as connection:
        connection.execute(
            "UPDATE vk_content_plan SET due_at=?,updated_at=? WHERE id=? AND status='approved'",
            (due_at, int(time.time()), item_id),
        )
    item = STORE.get(item_id)
    print(f"{item_id}: {item.status}, публикация в "
          f"{time.strftime('%H:%M', time.localtime(item.due_at))} — {item.content_type}")
