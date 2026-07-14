"""Ручное фото товара ответом на превью (выбор владельца 2026-07-14, вариант 1).

research-фото для прайс-позиций — рисунок модели по названию: для техники с
фирменным дизайном (бойлеры Ballu Shell) выходит непохожий «генерик». Владелец
отвечает на превью реальным фото → фото ложится в research_cache с
source='manual' (приоритет: research его не перезапишет) и позиция уходит на
перегенерацию карточки штатной regen-логикой. Тик соберёт карточку уже с
реальным товаром (submit_card понимает абсолютный путь фото)."""
from __future__ import annotations
from pathlib import Path

from content_factory.orchestrator.excel_pipeline import ExcelStore, _cache_key

_PREVIEW_ACTIONS = ("approve", "reject", "regen", "price")


def preview_code_from_reply(msg: dict) -> str | None:
    """Код позиции из reply на превью: у превью-сообщения inline-кнопки с
    callback_data «approve:<code>» — Telegram отдаёт их в reply_to_message."""
    rm = ((msg or {}).get("reply_to_message") or {}).get("reply_markup") or {}
    for row in rm.get("inline_keyboard") or []:
        for btn in row:
            data = btn.get("callback_data") or ""
            if ":" in data:
                action, code = data.split(":", 1)
                if action in _PREVIEW_ACTIONS:
                    return code
    return None


def make_manual_photo_fn(state_db, links, confirm_store, regen_fn, photos_dir):
    """manual_photo(msg, photo_bytes) → текст ответа, или None если фото не
    является ответом на превью (тогда его разбирает визард)."""
    store = ExcelStore(state_db)
    photos_dir = Path(photos_dir)

    def manual_photo(msg: dict, photo_bytes: bytes) -> str | None:
        code = preview_code_from_reply(msg)
        if code is None:
            return None
        key = links.key_for(code)
        if not key:
            return "❌ не нашёл превью по этой кнопке — ответь фото на сообщение превью"
        item = store.get(key)
        if item is None:
            return f"❌ позиции «{key}» нет в конвейере"

        photos_dir.mkdir(parents=True, exist_ok=True)
        p = photos_dir / f"manual_{code}.jpg"
        p.write_bytes(photo_bytes)

        cached = store.cache_get(_cache_key(item))
        utp = (cached[0] if cached else "") or ""
        store.cache_put(_cache_key(item), utp, str(p), source="manual")

        a = confirm_store.get(key)
        if a is not None:
            regen_fn(a)                       # карточка/card_jobs/excel_items → new
            confirm_store.mark(key, "regen")
        else:                                  # превью ещё не было — просто пересборка
            store.update(key, status="new", research_job=None, card_job=None, tries=0)
        return (f"📸 Фото принято: {item.name[:60]} — карточка будет перегенерирована "
                f"с реальным товаром и придёт новым превью")

    return manual_photo
