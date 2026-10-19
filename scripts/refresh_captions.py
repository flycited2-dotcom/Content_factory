"""Пересобирает тексты редакционных постов, созданных до появления крючков.

Записи, попавшие в план старым кодом, хранят текст в базе и сами не
обновятся. Скрипт сверяет каждый с текущей редакционной базой и переписывает
только те, что реально отличаются.
"""
import sqlite3
import sys

sys.path.insert(0, "/opt/content-factory-vk/src")
from content_factory.agents.editorial import (  # noqa: E402
    ResearchAgent, VkEditorialAgent, load_ideas, post_facts,
)
from content_factory.orchestrator.vk_content_plan import (  # noqa: E402
    VkContentPlanStore, editorial_asset_path,
)

DB = "/opt/content-factory-vk/state/vk-plan.db"
ASSETS = "/opt/content-factory-vk/assets/generated/editorial"
KNOWLEDGE = "/opt/content-factory-vk/config/vk-editorial-sources.yaml"

ideas, trusted = load_ideas(KNOWLEDGE)
by_id = {idea.id: idea for idea in ideas}
research, editor = ResearchAgent(trusted), VkEditorialAgent()
store = VkContentPlanStore(DB)

targets = [item for item in store.list()
           if item.source_key.startswith("editorial:")
           and item.status in {"planned", "review", "visual_pending"}]

for item in targets:
    idea_id = item.source_key.split(":")[1]
    idea = by_id.get(idea_id)
    if idea is None:
        print(f"{item.id} {idea_id}: темы больше нет — пропуск")
        continue
    text = editor.write(idea, post_facts(idea, research.verify(idea))).text
    if text == item.caption:
        continue
    path = editorial_asset_path(ASSETS, idea_id)
    if not path:
        print(f"{item.id} {idea_id}: нет кадра — пропуск")
        continue
    was_review = item.status == "review"
    if was_review:
        # Карточка ревью уже ушла со старым текстом: возвращаем в план,
        # чтобы владелец получил свежую вместо расходящейся с базой.
        with sqlite3.connect(DB) as connection:
            connection.execute(
                "UPDATE vk_content_plan SET status='planned',telegram_message_id=NULL "
                "WHERE id=? AND status='review'", (item.id,),
            )
    ok = store.update_editorial_content(item.id, text, path)
    mark = "обновлён" if ok else "ОТКАЗ (dedupe?)"
    print(f"{item.id} {idea_id}: {mark}{' + возвращён в план' if was_review else ''}")
