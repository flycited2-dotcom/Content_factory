"""Источник «почта» (модуль): забирает прайс .xlsx из IMAP-ящика владельца —
непрочитанные письма от MAIL_FROM_FILTER (1С шлёт с info@simfer.com.ru) — и кладёт
его в свой слот state/prices/mail.xlsx (отдельно от manual.xlsx — прайса,
загруженного владельцем вручную; /make и /find ищут в обоих, см. load_price_slots).
Сводка — владельцу в личку. Таймер cf-mail каждые 30 минут.

.env: MAIL_IMAP_HOST (imap.gmail.com), MAIL_IMAP_USER, MAIL_IMAP_PASSWORD
(app-password Gmail), MAIL_FROM_FILTER. Пустой пароль = модуль выключен.

  python -m content_factory.ingest.mail_ingest
"""
from __future__ import annotations
import email
import email.policy
import imaplib
from datetime import datetime
from pathlib import Path


def extract_xlsx_attachments(raw_mail: bytes) -> list[tuple[str, bytes]]:
    """Все .xlsx-вложения письма → [(имя, байты)]. (.xls 1997 openpyxl не читает —
    появится у поставщика, добавим конвертацию.)"""
    msg = email.message_from_bytes(raw_mail, policy=email.policy.default)
    out = []
    for part in msg.iter_attachments():
        fname = part.get_filename() or ""
        if fname.lower().endswith(".xlsx"):
            out.append((fname, part.get_payload(decode=True)))
    return out


def fetch_new_prices(host: str, user: str, password: str, from_filter: str):
    """Непрочитанные письма от отправителя → список (имя, байты) .xlsx-вложений.
    Письма помечаются прочитанными (fetch BODY[] ставит \\Seen)."""
    box = imaplib.IMAP4_SSL(host)
    try:
        box.login(user, password)
        box.select("INBOX")
        _, data = box.search(None, "UNSEEN", "FROM", f'"{from_filter}"')
        files = []
        for num in (data[0] or b"").split():
            _, msg_data = box.fetch(num, "(RFC822)")
            files.extend(extract_xlsx_attachments(msg_data[0][1]))
        return files
    finally:
        try:
            box.logout()
        except Exception:
            pass


def main():
    from decouple import config
    from content_factory.config import load_config
    from content_factory.ingest.excel_price import parse_price_xlsx
    from content_factory.publish.telegram import send_message

    password = config("MAIL_IMAP_PASSWORD", "")
    if not password:
        print("mail: выключен (нет MAIL_IMAP_PASSWORD в .env)")
        return
    cfg = load_config(Path("config/config.yaml"))
    files = fetch_new_prices(config("MAIL_IMAP_HOST", "imap.gmail.com"),
                             config("MAIL_IMAP_USER"), password,
                             config("MAIL_FROM_FILTER", "info@simfer.com.ru"))
    if not files:
        print("mail: новых прайсов нет")
        return
    pdir = Path(cfg.state.db).parent / "prices"
    pdir.mkdir(parents=True, exist_ok=True)
    token = config("TELEGRAM_BOT_TOKEN", "")
    owner = config("TELEGRAM_OWNER_CHAT_ID", config("FOTOGEN_CHAT_ID", ""))
    for fname, data in files:
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        (pdir / f"mail_{stamp}_{fname}").write_bytes(data)
        (pdir / "mail.xlsx").write_bytes(data)
        try:
            n = len(parse_price_xlsx(pdir / "mail.xlsx"))
            note = f"{n} позиций"
        except Exception as e:
            note = f"не парсится: {e}"
        if token and owner:
            send_message(token, owner,
                         f"📬 Прайс из почты «{fname}» принят: {note}.\n"
                         f"Дальше: /find <что ищем> или /make 5 <категория>")
        print(f"mail: принят {fname} ({note})")


if __name__ == "__main__":
    main()
