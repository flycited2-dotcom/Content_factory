"""Привязать сгенерированный визуал к редакционному VK-материалу.

Скрипт обновляет текст из проверенной базы знаний, dedupe-отпечаток и, если
материал уже находится на ревью, заменяет текстовое Telegram-превью фотопревью.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import httpx
from decouple import config

from content_factory.agents.editorial import (
    ResearchAgent,
    StrictCriticAgent,
    VkEditorialAgent,
    load_ideas,
)
from content_factory.analytics.vk import editorial_destination, tracked_caption
from content_factory.orchestrator.vk_content_plan import (
    DEFAULT_PLAN_DB,
    VkContentPlanStore,
    callback_markup,
    review_caption,
)
from content_factory.publish.telegram import publish_post


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Прикрепить редакционный визуал VK")
    parser.add_argument("plan_id", type=int)
    parser.add_argument("image")
    parser.add_argument("--state-db", default=os.getenv("VK_PLAN_STATE_DB", DEFAULT_PLAN_DB))
    parser.add_argument("--knowledge", default="config/vk-editorial-sources.yaml")
    parser.add_argument("--replace-review", action="store_true")
    args = parser.parse_args(argv)

    image = Path(args.image).resolve()
    store = VkContentPlanStore(args.state_db)
    item = store.get(args.plan_id)
    if item is None or not item.source_key.startswith("editorial:"):
        print(json.dumps({"ok": False, "error": "editorial plan item not found"}))
        return 1
    idea_id = item.source_key.split(":", 2)[1]
    ideas, trusted = load_ideas(args.knowledge)
    idea = next((value for value in ideas if value.id == idea_id), None)
    if idea is None:
        print(json.dumps({"ok": False, "error": f"idea {idea_id} not found"}))
        return 1
    facts = ResearchAgent(trusted).verify(idea)
    draft = VkEditorialAgent().write(idea, facts)
    verdict = StrictCriticAgent(trusted).review(idea, facts, draft)
    if not verdict.ok:
        print(json.dumps({"ok": False, "error": list(verdict.reasons)}, ensure_ascii=False))
        return 1
    if not store.update_editorial_content(
        item.id,
        draft.text,
        image,
        content_type=idea.content_type,
        category=idea.category,
    ):
        print(json.dumps({"ok": False, "error": "content update rejected"}))
        return 1

    replaced = False
    old_message_id = item.telegram_message_id
    if args.replace_review and item.status in {"review", "revision_requested"}:
        token = config("TELEGRAM_BOT_TOKEN", default="")
        chat_id = config("TELEGRAM_REVIEW_CHANNEL_ID", default="")
        body, _ = tracked_caption(
            draft.text, item.id, source_key=item.source_key,
            base_url=os.getenv("VK_SITE_URL", "https://splithome.ru/"),
            catalog_base_url=os.getenv("VK_CATALOG_SITE_URL", "https://climat-simf.ru/"),
            editorial_destination=editorial_destination(
                idea.category, idea.content_type,
            ),
        )
        client = httpx.Client(timeout=60)
        preview = publish_post(
            token, chat_id, str(image), review_caption(store.get(item.id) or item, body=body),
            http=client, reply_markup=callback_markup(item), retries=1,
        )
        if not preview.ok or preview.message_id is None:
            print(json.dumps({"ok": False, "error": preview.error or "preview failed"},
                             ensure_ascii=False))
            return 1
        fresh = store.get(item.id)
        if fresh and fresh.status == "planned":
            store.mark_review(item.id, preview.message_id)
        else:
            store.replace_review_message(item.id, preview.message_id)
        if old_message_id:
            client.post(
                f"https://api.telegram.org/bot{token}/deleteMessage",
                data={"chat_id": str(chat_id), "message_id": int(old_message_id)},
            )
        replaced = True

    print(json.dumps({
        "ok": True,
        "plan_id": item.id,
        "idea_id": idea_id,
        "image": str(image),
        "review_replaced": replaced,
        "status": (store.get(item.id) or item).status,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
