"""Отправка карточки в очередь фотоагента (POST /api/submit-job, mode=kbt).
Вынесено из closure excel_run.main() (2026-07-04): вторым вызывающим стал визард
/task (bot/wizard) — он ставит карточку в обход research (owner уже дал фото/УТП),
поэтому submit_card нужен как переиспользуемая функция, а не приватный closure."""
from __future__ import annotations
import re
import sqlite3
from pathlib import Path
import httpx


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:60]


def make_card_submitter(api: str, headers: dict, output_dir, owner_chat: str,
                        queue_db: str, http: httpx.Client | None = None):
    """submit_card(brand, model, utp, photo_path) -> id задачи в очереди фотоагента.
    photo_path — относительно output_dir (обычный research-результат) либо абсолютный
    путь (готовое фото, напр. от владельца в визарде)."""
    client = http or httpx.Client(timeout=60)
    out_dir = Path(output_dir)

    def _silence(input_filename: str) -> int:
        """Наши задачи не рассылает result_sender бота; вернуть id задачи."""
        con = sqlite3.connect(queue_db)
        try:
            row = con.execute("SELECT id FROM jobs WHERE input_filename=?",
                              (input_filename,)).fetchone()
            con.execute("UPDATE jobs SET result_sent=1 WHERE id=?", (row[0],))
            con.commit()
            return int(row[0])
        finally:
            con.close()

    def submit_card(brand: str, model: str, utp: str, photo_path) -> int:
        photo = out_dir / photo_path if not str(photo_path).startswith("/") \
            else Path(photo_path)
        r = client.post(f"{api.rstrip('/')}/api/submit-job", headers=headers,
                        data={"mode": "kbt", "specs": utp, "brand": brand, "model": model,
                              "chat_id": owner_chat or "0", "caption": ""},
                        files={"photo": (f"{slug(brand)}_{slug(model)}.png",
                                         photo.read_bytes(), "image/png")})
        r.raise_for_status()
        return _silence(r.json()["queued"])

    return submit_card
