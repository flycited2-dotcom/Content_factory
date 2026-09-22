"""Отправить три стабилизатора в закрытый Telegram-review без автопубликации.

После отправки работающий cf-bot обрабатывает кнопки approve/reject через общую state-БД.
Публичная публикация возможна только после явного нажатия владельцем кнопки approve.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import httpx
from decouple import Config, RepositoryEnv

from content_factory.config import load_config
from content_factory.content.render import render_caption
from content_factory.ingest.storefront_api import collect_storefront_offers
from content_factory.orchestrator.confirm_store import ConfirmStore
from content_factory.pricing.pricing import compute_price
from content_factory.publish.orders import OrderLinks
from content_factory.publish.telegram import PublishState, publish_post
from content_factory.review.rules import ReviewItem, review

DEFAULT_TARGETS_VA = (1500, 8000, 12000)
_VA_RE = re.compile(r"(?<!\d)(\d[\d\s]*(?:[.,]\d+)?)\s*(к?ВА)(?![А-Яа-я])", re.I)


def apparent_power_va(model: str) -> int | None:
    match = _VA_RE.search(model or "")
    if not match:
        return None
    value = float(match.group(1).replace(" ", "").replace(",", "."))
    if match.group(2).lower() == "ква":
        value *= 1000
    return int(round(value))


def select_pilot_offers(offers, targets=DEFAULT_TARGETS_VA, count: int = 3):
    """Детерминированно выбрать разные мощности, только с фото и валидной розничной ценой."""
    candidates = [o for o in offers if o.stock > 0 and o.photos and o.retail_ref and o.retail_ref > 0]
    selected = []
    used = set()
    for target in targets:
        ranked = sorted(
            (o for o in candidates if o.supplier_sku not in used and apparent_power_va(o.model)),
            key=lambda o: (abs(apparent_power_va(o.model) - target), o.brand.casefold(), o.model.casefold()),
        )
        if ranked:
            selected.append(ranked[0])
            used.add(ranked[0].supplier_sku)
        if len(selected) >= count:
            return selected
    for offer in sorted(candidates, key=lambda o: (o.brand.casefold(), o.model.casefold())):
        if offer.supplier_sku not in used:
            selected.append(offer)
            used.add(offer.supplier_sku)
        if len(selected) >= count:
            break
    return selected


def review_markup(code: str, approve_text: str = "✅ Опубликовать") -> str:
    return json.dumps({"inline_keyboard": [[
        {"text": approve_text, "callback_data": f"approve:{code}"},
        {"text": "❌ Отклонить", "callback_data": f"reject:{code}"},
    ]]}, ensure_ascii=False)


def retired_markup() -> str:
    return json.dumps({"inline_keyboard": [[
        {"text": "♻️ Заменено новой версией", "callback_data": "noop"},
    ]]}, ensure_ascii=False)


def _download_image(http: httpx.Client, url: str, path: Path) -> None:
    response = http.get(url)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()
    if content_type and not content_type.startswith("image/"):
        raise ValueError(f"URL фотографии вернул {content_type}")
    if not response.content:
        raise ValueError("Пустая фотография")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(response.content)


def absolute_media_path(path: Path) -> Path:
    """Review может запускаться из другого checkout, а бот — из /opt/content-factory.
    В state всегда сохраняем абсолютный путь, иначе approve ищет файл относительно CWD бота."""
    return Path(path).resolve()


def run_pilot(cfg, *, catalog_token: str, telegram_token: str, review_chat: str,
              publish_channel: str, state_db: Path, media_dir: Path, count: int = 3,
              http: httpx.Client | None = None, dry_run: bool = False,
              creative_dir: Path | None = None, refresh_existing: bool = False,
              retire_message_ids: tuple[int, ...] = (),
              sku_filter: tuple[str, ...] = ()) -> dict:
    if not dry_run and not all((telegram_token.strip(), review_chat.strip(), publish_channel.strip())):
        raise ValueError("Не заданы Telegram token/review chat/publish channel")
    owns_client = http is None
    client = http or httpx.Client(timeout=60, follow_redirects=True)
    store = None if dry_run else ConfirmStore(state_db)
    links = None if dry_run else OrderLinks(state_db)
    pub_state = None if dry_run else PublishState(state_db)
    summary = {"selected": 0, "prepared": [], "sent": [], "skipped": [],
               "retired": [], "errors": []}
    try:
        offers = collect_storefront_offers(cfg.source, catalog_token, client=client)
        if sku_filter:
            wanted = set(sku_filter)
            selected = [offer for offer in offers if offer.supplier_sku in wanted]
            selected.sort(key=lambda offer: sku_filter.index(offer.supplier_sku))
        else:
            selected = select_pilot_offers(offers, count=count)
        summary["selected"] = len(selected)
        if len(selected) < count:
            raise RuntimeError(f"Для пилота найдено только {len(selected)} подходящих товаров")
        for offer in selected:
            existing = store.get(offer.supplier_sku) if store else None
            if (not refresh_existing and existing
                    and existing.status in {"pending", "published", "rejected"}):
                summary["skipped"].append({"key": offer.supplier_sku, "status": existing.status})
                continue
            price = compute_price(offer, cfg.pricing)
            caption = render_caption(offer, price.price if price.ok else None, cfg.content)
            stem = offer.supplier_sku.replace(':', '_')
            creative_path = (creative_dir / f"{stem}.png") if creative_dir else None
            image_path = creative_path if creative_path and creative_path.is_file() \
                else media_dir / f"{stem}.jpg"
            try:
                if not (creative_path and creative_path.is_file()):
                    _download_image(client, offer.photos[0], image_path)
                image_path = absolute_media_path(image_path)
                ok, reasons = review(
                    ReviewItem(price=price.price if price.ok else None, caption=caption,
                               attrs=offer.attrs, card_path=str(image_path), brand=offer.brand,
                               category_id=offer.category_id),
                    cfg.review, cfg.content.stop_words,
                )
                if not ok:
                    raise ValueError("редактор: " + "; ".join(reasons))
                item_result = {"key": offer.supplier_sku,
                               "power_va": apparent_power_va(offer.model),
                               "price": price.price}
                if dry_run:
                    summary["prepared"].append(item_result)
                    continue
                published = next((r for r in pub_state.records()
                                  if r.key == offer.supplier_sku and r.message_id and r.channel), None)
                target = (f"edit|{published.channel}|{published.message_id}"
                          if published else publish_channel)
                store.add(offer.supplier_sku, target, str(image_path), caption)
                code = links.code_for(offer.supplier_sku)
                result = publish_post(
                    telegram_token, review_chat, str(image_path),
                    caption + "\n\n— закрытый пилот: требуется решение владельца —",
                    http=client, parse_mode=cfg.telegram.parse_mode, retries=2,
                    reply_markup=review_markup(
                        code, "✅ Заменить карточку" if published else "✅ Опубликовать"),
                )
                if not result.ok:
                    store.mark(offer.supplier_sku, "send_failed")
                    raise RuntimeError(result.error or "Telegram send failed")
                summary["sent"].append({**item_result, "message_id": result.message_id,
                                        "action": "replace" if published else "publish"})
            except (OSError, ValueError, RuntimeError, httpx.HTTPError) as exc:
                summary["errors"].append({"key": offer.supplier_sku, "error": str(exc)})
        if not dry_run and not summary["errors"]:
            for message_id in retire_message_ids:
                try:
                    response = client.post(
                        f"https://api.telegram.org/bot{telegram_token}/editMessageReplyMarkup",
                        data={"chat_id": review_chat, "message_id": int(message_id),
                              "reply_markup": retired_markup()},
                    )
                    body = response.json() or {}
                    if response.status_code == 200 and body.get("ok"):
                        summary["retired"].append(int(message_id))
                    else:
                        summary["errors"].append(
                            {"key": f"review:{message_id}",
                             "error": body.get("description") or f"http {response.status_code}"})
                except (httpx.HTTPError, ValueError) as exc:
                    summary["errors"].append({"key": f"review:{message_id}", "error": str(exc)})
        return summary
    finally:
        if owns_client:
            client.close()


def _env(path: str) -> Config:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(p)
    return Config(RepositoryEnv(str(p)))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/stabilizers-b2c.yaml")
    parser.add_argument("--catalog-env", required=True)
    parser.add_argument("--telegram-env", required=True)
    parser.add_argument("--catalog-token-key", default="TENDER_AGENT_API_TOKEN")
    parser.add_argument("--state-db", required=True)
    parser.add_argument("--media-dir", required=True)
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--creative-dir")
    parser.add_argument("--refresh-existing", action="store_true")
    parser.add_argument("--retire-review-message", type=int, action="append", default=[])
    parser.add_argument("--sku", action="append", default=[],
                        help="Отправить только указанный supplier_sku (можно повторять)")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    catalog_env = _env(args.catalog_env)
    telegram_env = _env(args.telegram_env)
    result = run_pilot(
        cfg,
        catalog_token=catalog_env(args.catalog_token_key, default=""),
        telegram_token=telegram_env("TELEGRAM_BOT_TOKEN", default=""),
        review_chat=telegram_env("TELEGRAM_REVIEW_CHANNEL_ID", default=""),
        publish_channel=telegram_env("TELEGRAM_CHANNEL_ID", default=""),
        state_db=Path(args.state_db),
        media_dir=Path(args.media_dir),
        count=max(1, args.count),
        dry_run=args.dry_run,
        creative_dir=Path(args.creative_dir) if args.creative_dir else None,
        refresh_existing=args.refresh_existing,
        retire_message_ids=tuple(args.retire_review_message),
        sku_filter=tuple(args.sku),
    )
    print(json.dumps(result, ensure_ascii=False))
    completed = len(result["prepared"]) + len(result["sent"]) + len(result["skipped"])
    if result["errors"] or completed < args.count:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
