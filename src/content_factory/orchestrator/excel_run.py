"""CLI-тик excel-конвейера (таймер cf-excel, каждые 10 мин): двигает товары прайса
по этапам research → card → preview. Адаптеры к реальному миру: очередь фотоагента
(HTTP submit + чтение queue.db), файлы карточек, превью в ревью-канал со штатными
кнопками ✅/❌/🔄 (публикацию по ✅ делает cf-bot, канал — боевой из .env).

  python -m content_factory.orchestrator.excel_run
"""
from __future__ import annotations
import html
import json
import os
import re
import shutil
import sqlite3
from pathlib import Path

import httpx
from decouple import config

from content_factory.config import load_config
from content_factory.orchestrator.excel_pipeline import ExcelStore, tick
from content_factory.orchestrator.confirm_store import ConfirmStore
from content_factory.orchestrator.card_submit import (
    assigned_account, make_card_submitter, slug as _slug,
)
from content_factory.publish.orders import OrderLinks
from content_factory.publish.telegram import publish_post, send_message
from content_factory.orchestrator.generation import generation_enabled
from content_factory.ready_price import enabled as ready_price_enabled, sync_catalog
from content_factory.ready_price_audit import audit_card_text

DIVIDER = "═" * 26


def _money(p) -> str:
    return f"{int(p):,}".replace(",", " ") + " ₽"


def build_preview_caption(name: str, price: int, utp: str) -> str:
    """Подпись превью. Названия из прайсов содержат <артикулы в скобках> —
    при parse_mode=HTML Telegram считает их битым тегом и отклоняет пост,
    поэтому экранируем (грабля чайников Vitek 2026-07-03). Цена — отдельной
    плашкой-цитатой (blockquote) с 💎 и жирным номиналом (2026-07-05)."""
    return (f"{html.escape(name)}\n<blockquote>💎 <b>{_money(price)}</b></blockquote>\n"
            f"{DIVIDER}\nКлючевые особенности:\n{html.escape(utp or '')}")


_PRICE_BLOCK_RE = re.compile(r"<blockquote>💎 <b>[^<]*</b></blockquote>")


def replace_price_in_caption(caption: str, new_price: int) -> str:
    """Заменить цену-плашку в готовой подписи превью (кнопка «💰 Изменить цену»,
    запрос владельца 2026-07-07: ручная цена для акций на единичный товар)."""
    return _PRICE_BLOCK_RE.sub(
        f"<blockquote>💎 <b>{_money(new_price)}</b></blockquote>", caption)


_ANY_PRICE_RE = re.compile(r"(\d[\d\s]*)\s*₽")
_HEAD_PRICE_RE = re.compile(r"<blockquote>💎 <b>(?:от )?([\d\s]+)\s*₽</b></blockquote>")


def scale_prices_in_caption(caption: str, new_price: int) -> str:
    """Ручная цена для СЕРИИ (авто-превью, запрос владельца 2026-07-09): новая
    заголовочная цена задаёт коэффициент, ВСЯ линейка «Модели и цены» сдвигается
    пропорционально (окончания …90 сохраняются) — иначе «от X» разъедется с
    линейкой. Нет заголовка/линейки — обычная замена одной цены."""
    from content_factory.pricing.pricing import round_up_90
    m = _HEAD_PRICE_RE.search(caption)
    old_head = int(re.sub(r"\s", "", m.group(1))) if m else 0
    if not old_head:
        return replace_price_in_caption(caption, new_price)
    k = new_price / old_head

    def _sub(pm):
        val = int(re.sub(r"\s", "", pm.group(1)))
        return f"{_money(round_up_90(val * k))}"
    return _ANY_PRICE_RE.sub(_sub, caption)


def preview_markup(code: str) -> dict:
    """Кнопки превью excel-товара: публикация/отклонение/перегенерация + ручная
    цена. Общая для excel_run (первичное превью) и bot (переотправка после
    смены цены) — единый вид, одна точка правки."""
    return {"inline_keyboard": [
        [{"text": "✅ Опубликовать", "callback_data": f"approve:{code}"},
         {"text": "❌ Отклонить", "callback_data": f"reject:{code}"}],
        [{"text": "🔄 Перегенерировать карточку", "callback_data": f"regen:{code}"}],
        [{"text": "💰 Изменить цену", "callback_data": f"price:{code}"}]]}


def save_ready_price_content(store: ExcelStore, item, card_output: str,
                             output_dir: Path, content_dir: Path) -> tuple[bool, str | None]:
    """Сохранить карточку и доказательства для Avito без внешней публикации."""
    article = item.key.split("|", 1)[1]
    if not re.fullmatch(r"[A-Za-zА-Яа-я0-9#._-]+", article):
        return False, "unsafe_article"
    evidence = store.cache_evidence(f"{item.brand.strip().lower()}|{item.model.strip().lower()}")
    source = output_dir / card_output
    cached = store.cache_get(f"{item.brand.strip().lower()}|{item.model.strip().lower()}")
    original = output_dir / cached[1] if cached and cached[1] else None
    if not evidence or not source.is_file() or original is None or not original.is_file():
        return False, "missing_content_inputs"
    audit = audit_card_text(
        source, original, brand=item.brand, model=item.model, name=item.name,
        features=list(evidence.get("features") or []),
    )
    if not audit.get("passed"):
        terms = ",".join(audit.get("unverified_terms") or [])
        reason = audit.get("reason") or f"unverified_card_text:{terms}"
        return False, reason
    target = content_dir / article
    target.mkdir(parents=True, exist_ok=True)
    card = target / "card.png"
    temp_card = target / ".card.png.tmp"
    shutil.copyfile(source, temp_card)
    os.replace(temp_card, card)
    original_target = target / "original.png"
    temp_original = target / ".original.png.tmp"
    shutil.copyfile(original, temp_original)
    os.replace(temp_original, original_target)
    manifest = {"schema_version": 1, "article": article, "brand": item.brand,
                "model": item.model, "name": item.name, "price": item.price,
                "card_mode": item.card_mode, "card": "card.png",
                "original": "original.png", "evidence": evidence,
                "card_text_audit": audit}
    temp_manifest = target / ".content.json.tmp"
    temp_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    os.replace(temp_manifest, target / "content.json")
    return True, None


def telegram_starting_items(store: ExcelStore, global_enabled: bool) -> list[dict]:
    """Только реально запускаемые ручные задачи, которым придёт Telegram-превью."""
    if not global_enabled:
        return []
    return [item for item in store.due_scheduled()
            if not item["key"].startswith("ready-price|")]


def main():
    cfg = load_config(Path("config/config.yaml"))
    global_enabled = generation_enabled(cfg.state.db)
    source_enabled = ready_price_enabled(cfg.state.db)
    if not global_enabled and not source_enabled:
        print("excel: generation disabled by master switch")
        return
    sync_result = None
    if source_enabled:
        catalog = Path(config("READY_PRICE_CATALOG_DB",
                              "/opt/avito-bridge/state/ready-price/catalog.sqlite"))
        if catalog.is_file():
            sync_result = sync_catalog(catalog, cfg.state.db)
    store = ExcelStore(cfg.state.db)
    api = config("FOTOGEN_API_URL", cfg.fotogen.api_url).rstrip("/")
    headers = {"x-agent-token": config("FOTOGEN_API_TOKEN")}
    queue_db = config("FOTOGEN_QUEUE_DB")
    output_dir = Path(config("FOTOGEN_OUTPUT_DIR"))
    owner_chat = config("TELEGRAM_OWNER_CHAT_ID", config("FOTOGEN_CHAT_ID", ""))
    token = config("TELEGRAM_BOT_TOKEN", "")
    review = config("TELEGRAM_REVIEW_CHANNEL_ID", cfg.telegram.review_channel_id)
    channel = config("TELEGRAM_CHANNEL_ID", cfg.telegram.channel_id)
    markup_pct = float(config("EXCEL_MARKUP_PCT", "0"))
    http = httpx.Client(timeout=60)
    cs = ConfirmStore(cfg.state.db)
    links = OrderLinks(cfg.state.db)

    def submit_research(brand, model, category):
        r = http.post(f"{api}/api/submit-research", headers=headers,
                      data={"brand": brand, "model": model, "category": category,
                            "chat_id": owner_chat or "0",
                            "assigned_account": assigned_account(
                                brand, model, queue_db=queue_db)})
        r.raise_for_status()
        return int(r.json()["job_id"])

    def read_job(job_id):
        con = sqlite3.connect(f"file:{queue_db}?mode=ro", uri=True)
        row = con.execute("SELECT status, output_filename, result_specs, error_text "
                          "FROM jobs WHERE id=?", (job_id,)).fetchone()
        con.close()
        return row if row else ("pending", None, None, None)

    submit_card = make_card_submitter(api, headers, output_dir, owner_chat,
                                      queue_db, http=http)

    def _alert(text):
        if token and owner_chat:
            send_message(token, owner_chat, text, http=http)

    def preview(item, card_output):
        if item.key.startswith("ready-price|"):
            return save_ready_price_content(
                store, item, card_output, output_dir,
                Path(config("READY_PRICE_CONTENT_DIR", "/opt/avito-ready-price/content")))
        card = f"{cfg.cards.dir}/excel_{_slug(item.brand)}-{_slug(item.model)}.jpg"
        shutil.copyfile(output_dir / card_output, card)
        utp = (store.cache_get(f"{item.brand.strip().lower()}|{item.model.strip().lower()}")
               or ("", None))[0]
        price = int(round(item.price * (1 + markup_pct / 100)))
        caption = build_preview_caption(item.name, price, utp or "")
        cs.add(item.key, channel, card, caption)
        # excel-ключи длинные → в callback_data короткий код (бот развернёт обратно)
        code = links.code_for(item.key)
        kb = json.dumps(preview_markup(code), ensure_ascii=False)
        res = publish_post(token, review, card, f"{caption}\n\n— на подтверждение —",
                           http=http, parse_mode=cfg.telegram.parse_mode, reply_markup=kb)
        if not res.ok:                    # не молчим: владелец должен видеть сбой
            _alert(f"⚠️ Превью «{item.name[:60]}» не отправилось: {res.error}")
        return bool(res.ok)

    # Дозревшие отложенные позиции снимаем ДО тика (после — уже research);
    # владелец должен видеть старт «задачи к 9:00», а не тишину (2026-07-10)
    # ready-price работает полностью автоматически и сохраняет карточки прямо в
    # Avito-контент. Для него Telegram-превью не предусмотрены, поэтому нельзя
    # обещать их владельцу каждые 10 минут. Здесь остаются только ручные /task.
    starting = telegram_starting_items(store, global_enabled)
    active_ready = sum(1 for status in ("research", "card")
                       for item in store.by_status(status)
                       if item.key.startswith("ready-price|"))
    ready_budget = max(0, int(config("READY_PRICE_MAX_ACTIVE", "6")) - active_ready)
    # При отдельном источнике старые Excel-задания не оживляем. Синхронизация
    # создаёт ready-price раньше остальных new, а лимит не даёт заполнить очередь.
    max_new = ready_budget if source_enabled and not global_enabled else None
    stats = tick(store, submit_research, read_job, submit_card, preview, max_new=max_new,
                 new_key_prefix="ready-price|" if source_enabled and not global_enabled else None)
    if starting:
        _alert(f"⏳ Стартовала отложенная генерация: {len(starting)} позиций — "
               f"превью будут приходить по мере готовности. Статус: /excel")
    if stats["failed"]:
        fails = "\n".join(f"— {i.name[:60]}: {i.error}" for i in store.by_status("failed")[-5:])
        _alert(f"❌ Выпали из конвейера прайса ({stats['failed']}):\n{fails}")
    in_flight = sum(len(store.by_status(s)) for s in ("new", "research", "card"))
    if stats["preview"] and in_flight == 0:      # партия доехала до конца
        done = len(store.by_status("preview"))
        failed = len(store.by_status("failed"))
        _alert(f"🏁 Партия прайса обработана: превью {done}"
               + (f", не дошло {failed} (см. /excel)" if failed else "") + ".")
    sync_text = f" | sync {sync_result}" if sync_result is not None else ""
    print(f"excel: research {stats['research']} | card {stats['card']} | "
          f"preview {stats['preview']} | failed {stats['failed']}{sync_text}")


if __name__ == "__main__":
    main()
