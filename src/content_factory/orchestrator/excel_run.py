"""CLI-тик excel-конвейера (таймер cf-excel, каждые 10 мин): двигает товары прайса
по этапам research → card → preview. Адаптеры к реальному миру: очередь фотоагента
(HTTP submit + чтение queue.db), файлы карточек, превью в ревью-канал со штатными
кнопками ✅/❌/🔄 (публикацию по ✅ делает cf-bot, канал — боевой из .env).

  python -m content_factory.orchestrator.excel_run
"""
from __future__ import annotations
import json
import re
import shutil
import sqlite3
from pathlib import Path

import httpx
from decouple import config

from content_factory.config import load_config
from content_factory.orchestrator.excel_pipeline import ExcelStore, tick
from content_factory.orchestrator.confirm_store import ConfirmStore
from content_factory.publish.telegram import publish_post

DIVIDER = "═" * 26


def _money(p) -> str:
    return f"{int(p):,}".replace(",", " ") + " ₽"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


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

    def _silence(input_filename: str) -> int:
        """Наши задачи не рассылает result_sender бота; вернуть id задачи."""
        con = sqlite3.connect(queue_db)
        row = con.execute("SELECT id FROM jobs WHERE input_filename=?",
                          (input_filename,)).fetchone()
        con.execute("UPDATE jobs SET result_sent=1 WHERE id=?", (row[0],))
        con.commit()
        con.close()
        return int(row[0])

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

    def submit_card(brand, model, utp, photo_path):
        photo = output_dir / photo_path if not str(photo_path).startswith("/") \
            else Path(photo_path)
        r = http.post(f"{api}/api/submit-job", headers=headers,
                      data={"mode": "kbt", "specs": utp, "brand": brand, "model": model,
                            "chat_id": owner_chat or "0", "caption": ""},
                      files={"photo": (f"{_slug(brand)}_{_slug(model)}.png",
                                       photo.read_bytes(), "image/png")})
        r.raise_for_status()
        return _silence(r.json()["queued"])

    def preview(item, card_output):
        card = f"{cfg.cards.dir}/excel_{_slug(item.brand)}-{_slug(item.model)}.jpg"
        shutil.copyfile(output_dir / card_output, card)
        utp = (store.cache_get(f"{item.brand.strip().lower()}|{item.model.strip().lower()}")
               or ("", None))[0]
        price = int(round(item.price * (1 + markup_pct / 100)))
        caption = (f"{item.name}\n💰 {_money(price)}\n{DIVIDER}\n"
                   f"Ключевые особенности:\n{utp}")
        cs.add(item.key, channel, card, caption)
        kb = json.dumps({"inline_keyboard": [
            [{"text": "✅ Опубликовать", "callback_data": f"approve:{item.key}"},
             {"text": "❌ Отклонить", "callback_data": f"reject:{item.key}"}],
            [{"text": "🔄 Перегенерировать карточку", "callback_data": f"regen:{item.key}"}]]},
            ensure_ascii=False)
        res = publish_post(token, review, card, f"{caption}\n\n— на подтверждение —",
                           http=http, parse_mode=cfg.telegram.parse_mode, reply_markup=kb)
        return bool(res.ok)

    stats = tick(store, submit_research, read_job, submit_card, preview)
    print(f"excel: research {stats['research']} | card {stats['card']} | "
          f"preview {stats['preview']} | failed {stats['failed']}")


if __name__ == "__main__":
    main()
