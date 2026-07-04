"""CLI-тик excel-конвейера (таймер cf-excel, каждые 10 мин): двигает товары прайса
по этапам research → card → preview. Адаптеры к реальному миру: очередь фотоагента
(HTTP submit + чтение queue.db), файлы карточек, превью в ревью-канал со штатными
кнопками ✅/❌/🔄 (публикацию по ✅ делает cf-bot, канал — боевой из .env).

  python -m content_factory.orchestrator.excel_run
"""
from __future__ import annotations
import html
import json
import shutil
import sqlite3
from pathlib import Path

import httpx
from decouple import config

from content_factory.config import load_config
from content_factory.orchestrator.excel_pipeline import ExcelStore, tick
from content_factory.orchestrator.confirm_store import ConfirmStore
from content_factory.orchestrator.card_submit import make_card_submitter, slug as _slug
from content_factory.publish.orders import OrderLinks
from content_factory.publish.telegram import publish_post, send_message

DIVIDER = "═" * 26


def _money(p) -> str:
    return f"{int(p):,}".replace(",", " ") + " ₽"


def build_preview_caption(name: str, price: int, utp: str) -> str:
    """Подпись превью. Названия из прайсов содержат <артикулы в скобках> —
    при parse_mode=HTML Telegram считает их битым тегом и отклоняет пост,
    поэтому экранируем (грабля чайников Vitek 2026-07-03)."""
    return (f"{html.escape(name)}\n💰 {_money(price)}\n{DIVIDER}\n"
            f"Ключевые особенности:\n{html.escape(utp or '')}")


def main():
    cfg = load_config(Path("config/config.yaml"))
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
                            "chat_id": owner_chat or "0"})
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
        card = f"{cfg.cards.dir}/excel_{_slug(item.brand)}-{_slug(item.model)}.jpg"
        shutil.copyfile(output_dir / card_output, card)
        utp = (store.cache_get(f"{item.brand.strip().lower()}|{item.model.strip().lower()}")
               or ("", None))[0]
        price = int(round(item.price * (1 + markup_pct / 100)))
        caption = build_preview_caption(item.name, price, utp or "")
        cs.add(item.key, channel, card, caption)
        # excel-ключи длинные → в callback_data короткий код (бот развернёт обратно)
        code = links.code_for(item.key)
        kb = json.dumps({"inline_keyboard": [
            [{"text": "✅ Опубликовать", "callback_data": f"approve:{code}"},
             {"text": "❌ Отклонить", "callback_data": f"reject:{code}"}],
            [{"text": "🔄 Перегенерировать карточку", "callback_data": f"regen:{code}"}]]},
            ensure_ascii=False)
        res = publish_post(token, review, card, f"{caption}\n\n— на подтверждение —",
                           http=http, parse_mode=cfg.telegram.parse_mode, reply_markup=kb)
        if not res.ok:                    # не молчим: владелец должен видеть сбой
            _alert(f"⚠️ Превью «{item.name[:60]}» не отправилось: {res.error}")
        return bool(res.ok)

    stats = tick(store, submit_research, read_job, submit_card, preview)
    if stats["failed"]:
        fails = "\n".join(f"— {i.name[:60]}: {i.error}" for i in store.by_status("failed")[-5:])
        _alert(f"❌ Выпали из конвейера прайса ({stats['failed']}):\n{fails}")
    in_flight = sum(len(store.by_status(s)) for s in ("new", "research", "card"))
    if stats["preview"] and in_flight == 0:      # партия доехала до конца
        done = len(store.by_status("preview"))
        failed = len(store.by_status("failed"))
        _alert(f"🏁 Партия прайса обработана: превью {done}"
               + (f", не дошло {failed} (см. /excel)" if failed else "") + ".")
    print(f"excel: research {stats['research']} | card {stats['card']} | "
          f"preview {stats['preview']} | failed {stats['failed']}")


if __name__ == "__main__":
    main()
