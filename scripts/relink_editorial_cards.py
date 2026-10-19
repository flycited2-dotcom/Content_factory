"""Привязывает к записям плана актуальные кадры тем и возвращает на ревью карточки со старой картинкой.

Запись плана хранит путь к картинке на момент постановки. Когда кадр темы
сменился (тёмную карточку заменила фотография), путь в записи указывает на
удалённый файл. Скрипт обновляет путь; если карточка уже уходила владельцу
со старой картинкой, возвращает её в «planned» без номера сообщения, чтобы
планировщик прислал её заново. Идемпотентен.

    python scripts/relink_editorial_cards.py
"""
import sys
from pathlib import Path

ROOT = Path("/opt/content-factory-vk")
sys.path.insert(0, str(ROOT / "src"))

from content_factory.orchestrator.vk_content_plan import (  # noqa: E402
    VkContentPlanStore, editorial_asset_path)

store = VkContentPlanStore(str(ROOT / "state" / "vk-plan.db"))
asset_root = ROOT / "assets" / "generated" / "editorial"
relinked = resent = 0
for item in store.list():
    if not item.source_key.startswith("editorial:"):
        continue
    if item.status not in {"planned", "review", "visual_pending"}:
        continue
    fresh = editorial_asset_path(asset_root, item.source_key.split(":")[1])
    if not fresh:
        continue
    with store._connect() as connection:
        sent_at = connection.execute(
            "SELECT updated_at FROM vk_content_plan WHERE id=?", (item.id,)).fetchone()[0]
    # Новый кадр ложится под прежним именем файла: путь совпадает, а карточка в
    # Telegram осталась со старой картинкой. Сравниваем время файла и отправки.
    stale_card = item.status == "review" and Path(fresh).stat().st_mtime > sent_at
    if fresh == item.card_path and not stale_card:
        continue
    returned = item.status == "review"
    with store._connect() as connection:
        connection.execute(
            "UPDATE vk_content_plan SET card_path=?,"
            "status=CASE WHEN status='review' THEN 'planned' ELSE status END,"
            "telegram_message_id=CASE WHEN status='review' THEN NULL ELSE telegram_message_id END "
            "WHERE id=? AND status IN ('planned','review','visual_pending')",
            (fresh, item.id))
    relinked += 1
    resent += int(returned)
    print(f"{item.id} {item.source_key.split(':')[1]}: новый кадр"
          f"{', карточка вернётся на ревью' if returned else ''}")
print(f"итого: обновлено {relinked}, вернётся на ревью {resent}")
