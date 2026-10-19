"""Обновить существующие редакционные записи VK без создания дублей."""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from decouple import config

from content_factory.agents.editorial import (
    ResearchAgent, StrictCriticAgent, VkEditorialAgent, load_ideas,
)
from content_factory.analytics.vk import editorial_destination, tracked_caption
from content_factory.orchestrator.vk_content_plan import DEFAULT_PLAN_DB, VkContentPlanStore
from content_factory.publish.vk import VkPublisher


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan_ids", nargs="+", type=int)
    parser.add_argument("--state-db", default=os.getenv("VK_PLAN_STATE_DB", DEFAULT_PLAN_DB))
    parser.add_argument("--knowledge", default="config/vk-editorial-sources.yaml")
    parser.add_argument("--owner-id", type=int, default=int(os.getenv("VK_OWNER_ID", "-241020718")))
    parser.add_argument(
        "--db-only",
        action="store_true",
        help="Обновить текст и метаданные плана после ручного редактирования поста VK",
    )
    args = parser.parse_args(argv)

    store = VkContentPlanStore(args.state_db)
    ideas, trusted = load_ideas(args.knowledge)
    idea_map = {idea.id: idea for idea in ideas}
    publisher = None if args.db_only else VkPublisher(
        config("VK_ACCESS_TOKEN", default=""), args.owner_id, dry_run=False,
    )
    results = []
    for plan_id in args.plan_ids:
        item = store.get(plan_id)
        if item is None or not item.source_key.startswith("editorial:") or not item.vk_post_id:
            results.append({"plan_id": plan_id, "ok": False, "error": "existing VK post not found"})
            continue
        idea_id = item.source_key.split(":", 2)[1]
        idea = idea_map.get(idea_id)
        if idea is None:
            results.append({"plan_id": plan_id, "ok": False, "error": "idea not found"})
            continue
        facts = ResearchAgent(trusted).verify(idea)
        draft = VkEditorialAgent().write(idea, facts)
        verdict = StrictCriticAgent(trusted).review(idea, facts, draft)
        if not verdict.ok:
            results.append({"plan_id": plan_id, "ok": False, "error": list(verdict.reasons)})
            continue
        body, _ = tracked_caption(
            draft.text, item.id, source_key=item.source_key,
            base_url=os.getenv("VK_SITE_URL", "https://splithome.ru/"),
            catalog_base_url=os.getenv("VK_CATALOG_SITE_URL", "https://climat-simf.ru/"),
            editorial_destination=editorial_destination(idea.category, idea.content_type),
        )
        if publisher is not None:
            publish_at = item.due_at if item.due_at > int(time.time()) else None
            edited = publisher.edit_text(item.vk_post_id, body, publish_at=publish_at)
            if not edited.ok:
                results.append({"plan_id": plan_id, "ok": False, "error": edited.error})
                continue
        updated = store.update_editorial_content(
            item.id, draft.text, Path(item.card_path),
            content_type=idea.content_type, category=idea.category,
        )
        results.append({
            "plan_id": plan_id, "post_id": item.vk_post_id,
            "ok": bool(updated), "points": len(facts),
            "destination": editorial_destination(idea.category, idea.content_type),
            "db_only": args.db_only,
        })
    print(json.dumps(results, ensure_ascii=False))
    return 0 if all(row.get("ok") for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
