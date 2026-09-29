"""Отправка карточки в очередь фотоагента (POST /api/submit-job, mode=kbt).
Вынесено из closure excel_run.main() (2026-07-04): вторым вызывающим стал визард
/task (bot/wizard) — он ставит карточку в обход research (owner уже дал фото/УТП),
поэтому submit_card нужен как переиспользуемая функция, а не приватный closure."""
from __future__ import annotations
import hashlib
import os
import re
import sqlite3
from pathlib import Path
import httpx


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:60]


def assigned_account(brand: str, model: str, accounts: str | None = None,
                     queue_db: str | None = None) -> str:
    """Стабильно закрепить модель; прежняя дорожка важнее нового хеша."""
    if queue_db:
        try:
            with sqlite3.connect(f"file:{queue_db}?mode=ro", uri=True) as con:
                row = con.execute(
                    "SELECT assigned_account FROM jobs WHERE brand=? COLLATE NOCASE "
                    "AND model=? COLLATE NOCASE AND COALESCE(assigned_account,'')<>'' "
                    "ORDER BY id DESC LIMIT 1", (brand.strip(), model.strip())).fetchone()
            if row and row[0]:
                return str(row[0])
        except sqlite3.Error:
            pass
    names = [x.strip() for x in (accounts if accounts is not None else
                                  os.getenv("FOTOGEN_ACCOUNTS", "acc1,acc2")).split(",")
             if x.strip()]
    if not names:
        return ""
    identity = f"{brand.strip().casefold()}|{model.strip().casefold()}".encode("utf-8")
    return names[int.from_bytes(hashlib.sha256(identity).digest()[:8], "big") % len(names)]


def make_card_submitter(api: str, headers: dict, output_dir, owner_chat: str,
                        queue_db: str, http: httpx.Client | None = None):
    """submit_card(brand, model, utp, photo_path) -> id задачи в очереди фотоагента.
    photo_path — относительно output_dir (обычный research-результат) либо абсолютный
    путь (готовое фото, напр. от владельца в визарде)."""
    client = http or httpx.Client(timeout=60)
    out_dir = Path(output_dir)

    def _silence(input_filename: str, job_id: int | None = None) -> int:
        """Наши задачи не рассылает result_sender бота; вернуть id задачи."""
        con = sqlite3.connect(queue_db)
        try:
            if job_id is None:
                row = con.execute("SELECT id FROM jobs WHERE input_filename=?",
                                  (input_filename,)).fetchone()
                job_id = int(row[0])
            con.execute("UPDATE jobs SET result_sent=1 WHERE id=?", (job_id,))
            con.commit()
            return int(job_id)
        finally:
            con.close()

    def submit_card(brand: str, model: str, utp: str, photo_path,
                    mode: str = "kbt", *, request_key: str = "") -> int:
        photo = out_dir / photo_path if not str(photo_path).startswith("/") \
            else Path(photo_path)
        r = client.post(f"{api.rstrip('/')}/api/submit-job", headers=headers,
                        data={"mode": mode, "specs": utp, "brand": brand, "model": model,
                              "chat_id": owner_chat or "0", "caption": "",
                              "request_key": request_key,
                              "assigned_account": assigned_account(
                                  brand, model, queue_db=queue_db)},
                        files={"photo": (f"{slug(brand)}_{slug(model)}.png",
                                         photo.read_bytes(), "image/png")})
        r.raise_for_status()
        data = r.json()
        return _silence(data["queued"], data.get("job_id"))

    return submit_card
