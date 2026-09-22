"""Сформировать JSON-превью VK из записи Telegram-review, ничего не публикуя."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from content_factory.config import load_config
from content_factory.orchestrator.confirm_store import ConfirmStore
from content_factory.publish.vk import (
    VkPublisher,
    build_stabilizer_vk_comment,
    build_vk_share_url,
)


def build_vk_preview(cfg, awaiting) -> dict:
    if not cfg.vk.enabled:
        raise ValueError("VK выключен в конфигурации")
    if not cfg.vk.dry_run:
        raise ValueError("preview разрешён только при vk.dry_run=true")
    payload = VkPublisher("", cfg.vk.owner_id, api_version=cfg.vk.api_version,
                          dry_run=True).preview(awaiting.card_path, awaiting.caption)
    if cfg.vk.share_url and cfg.vk.public_image_base_url:
        card_stem = Path(awaiting.card_path).stem
        share_slug = card_stem.replace("_", "-")
        image_url = f"{cfg.vk.public_image_base_url.rstrip('/')}/{Path(awaiting.card_path).name}"
        share_page_url = f"{cfg.vk.share_url.rstrip('/')}/{share_slug}"
        payload["message"] = build_stabilizer_vk_comment(payload["message"])
        lines = payload["message"].splitlines()
        payload["public_image_url"] = image_url
        payload["share_page_url"] = share_page_url
        payload["share_url"] = build_vk_share_url(
            url=share_page_url,
            title=lines[0] if lines else "Content Factory",
            description=payload["message"],
            image_url=image_url,
        )
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--state-db", required=True)
    parser.add_argument("--sku", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    awaiting = ConfirmStore(args.state_db).get(args.sku)
    if awaiting is None:
        raise SystemExit(f"Нет review-записи: {args.sku}")
    payload = build_vk_preview(cfg, awaiting)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
