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
from email.utils import parseaddr
import imaplib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import unicodedata


@dataclass(frozen=True)
class MailPriceMessage:
    sender: str
    subject: str
    message_id: str
    mail_date: str
    attachments: tuple[tuple[str, bytes], ...]
    imap_uid: str = ""


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


def extract_price_message(raw_mail: bytes) -> MailPriceMessage:
    """Извлечь аудируемый контекст письма и поддерживаемые прайс-вложения."""
    msg = email.message_from_bytes(raw_mail, policy=email.policy.default)
    sender = parseaddr(str(msg.get("From") or ""))[1].casefold().strip()
    return MailPriceMessage(
        sender=sender,
        subject=str(msg.get("Subject") or "").strip(),
        message_id=str(msg.get("Message-ID") or "").strip(),
        mail_date=str(msg.get("Date") or "").strip(),
        attachments=tuple(extract_xlsx_attachments(raw_mail)),
    )


def fetch_new_price_messages(
    host: str,
    user: str,
    password: str,
    from_filters: list[str],
    processed_uids: set[str] | None = None,
    backfill_limit: int = 100,
) -> list[MailPriceMessage]:
    """Новые XLSX-прайсы только от разрешённых отправителей.

    Каждый отправитель ищется серверным IMAP-фильтром. Это не скрапинг страниц и
    не передача почтового пароля в MCP: синхронизация выполняется отдельным job.
    """
    allowed = [value.casefold().strip() for value in from_filters if value.strip()]
    if not allowed:
        return []
    box = imaplib.IMAP4_SSL(host)
    try:
        box.login(user, password)
        box.select("INBOX")
        message_numbers: set[bytes] = set()
        for sender in allowed:
            _, data = box.uid("search", None, "FROM", f'"{sender}"')
            found = (data[0] or b"").split()
            message_numbers.update(found[-max(1, int(backfill_limit)):])
        already_done = processed_uids or set()
        message_numbers = {uid for uid in message_numbers if uid.decode("ascii", "ignore") not in already_done}
        messages = []
        for num in sorted(message_numbers, key=lambda value: int(value)):
            _, msg_data = box.uid("fetch", num, "(BODY.PEEK[])")
            parsed = extract_price_message(msg_data[0][1])
            if parsed.attachments and parsed.sender in allowed:
                messages.append(
                    MailPriceMessage(
                        sender=parsed.sender,
                        subject=parsed.subject,
                        message_id=parsed.message_id,
                        mail_date=parsed.mail_date,
                        attachments=parsed.attachments,
                        imap_uid=num.decode("ascii", "ignore"),
                    )
                )
        return messages
    finally:
        try:
            box.logout()
        except Exception:
            pass


def fetch_new_prices(host: str, user: str, password: str, from_filter: str):
    """Непрочитанные письма от отправителя → список (имя, байты) .xlsx-вложений.
    Письма помечаются прочитанными (fetch BODY[] ставит \\Seen)."""
    return [
        attachment
        for message in fetch_new_price_messages(host, user, password, [from_filter])
        for attachment in message.attachments
    ]


_SLOT_RE = re.compile(r"[^a-z0-9а-яё]+")


def mail_slot_name(sender: str, filename: str) -> str:
    """Стабильный отдельный слот: один поставщик/тип прайса не затирает другой."""
    raw = f"{sender}__{Path(filename or 'price').stem}"
    slug = _SLOT_RE.sub("_", unicodedata.normalize("NFC", raw).casefold()).strip("_")[:100]
    return f"mail__{slug or 'price'}"


def store_price_message(prices_dir: Path, message: MailPriceMessage) -> list[Path]:
    prices_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = prices_dir / "mail_sources.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
    except json.JSONDecodeError:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    saved = []
    now = datetime.now().astimezone()
    stamp = now.strftime("%Y%m%d_%H%M%S")
    for filename, data in message.attachments:
        slot = mail_slot_name(message.sender, filename)
        archive = prices_dir / f"archive__{stamp}__{slot}.xlsx"
        current = prices_dir / f"{slot}.xlsx"
        archive.write_bytes(data)
        temporary = prices_dir / f".{slot}.{stamp}.tmp"
        temporary.write_bytes(data)
        temporary.replace(current)
        metadata[slot] = {
            "sender": message.sender,
            "subject": message.subject,
            "message_id": message.message_id,
            "imap_uid": message.imap_uid,
            "mail_date": message.mail_date,
            "filename": filename,
            "updated_at": now.isoformat(timespec="seconds"),
            "archive": archive.name,
        }
        saved.append(current)
    metadata_temporary = prices_dir / ".mail_sources.json.tmp"
    metadata_temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    metadata_temporary.replace(metadata_path)
    return saved


def latest_price_messages(messages: list[MailPriceMessage]) -> list[MailPriceMessage]:
    """Оставить последнее письмо каждого поставщика со всеми его прайсами.

    При первичной загрузке в почте могут быть десятки писем с одним и тем же
    прайсом, причём дата нередко входит в имя файла. Тендерному каталогу нужна
    последняя рассылка поставщика, но все вложения этой рассылки (например,
    разные бренды) должны сохраниться.
    """
    latest: dict[str, MailPriceMessage] = {}
    for message in messages:
        latest[message.sender] = message
    return list(latest.values())


def deactivate_superseded_mail_slots(prices_dir: Path, messages: list[MailPriceMessage]) -> list[Path]:
    """Убрать старые активные прайсы тех же отправителей в восстановимый архив."""
    active_by_sender = {
        message.sender: {mail_slot_name(message.sender, filename) for filename, _ in message.attachments}
        for message in messages
    }
    moved: list[Path] = []
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    for sender, active_slots in active_by_sender.items():
        sender_prefix = mail_slot_name(sender, "price").removesuffix("price")
        for path in prices_dir.glob(f"{sender_prefix}*.xlsx"):
            if path.stem in active_slots:
                continue
            archived = prices_dir / f"archive__superseded__{stamp}__{path.name}"
            path.replace(archived)
            moved.append(archived)
    return moved


def _write_processed_uids(path: Path, values: set[str]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(sorted(values, key=lambda value: int(value)), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


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
    pdir = Path(cfg.state.db).parent / "prices"
    pdir.mkdir(parents=True, exist_ok=True)
    configured_filters = config(
        "MAIL_FROM_FILTERS",
        config("MAIL_FROM_FILTER", "info@simfer.com.ru"),
    )
    filters = [value.strip() for value in re.split(r"[,;\n]+", configured_filters) if value.strip()]
    processed_path = pdir / "mail_processed_uids.json"
    try:
        processed_uids = set(json.loads(processed_path.read_text(encoding="utf-8"))) if processed_path.is_file() else set()
    except (json.JSONDecodeError, TypeError):
        processed_uids = set()
    messages = fetch_new_price_messages(
        config("MAIL_IMAP_HOST", "imap.gmail.com"),
        config("MAIL_IMAP_USER"),
        password,
        filters,
        processed_uids=processed_uids,
        backfill_limit=int(config("MAIL_BACKFILL_LIMIT", "100")),
    )
    if not messages:
        print("mail: новых прайсов нет")
        return
    token = config("TELEGRAM_BOT_TOKEN", "")
    owner = config("TELEGRAM_OWNER_CHAT_ID", config("FOTOGEN_CHAT_ID", ""))
    notify_telegram = str(config("MAIL_NOTIFY_TELEGRAM", "false")).casefold() in {"1", "true", "yes", "on"}
    summaries: list[str] = []
    selected_messages = latest_price_messages(messages)
    deactivated = deactivate_superseded_mail_slots(pdir, selected_messages)
    if deactivated:
        print(f"mail: старых активных прайсов перенесено в архив: {len(deactivated)}")
    for message in selected_messages:
        saved = store_price_message(pdir, message)
        for path in saved:
            try:
                n = len(parse_price_xlsx(path))
                note = f"{n} позиций"
            except Exception as e:
                note = f"не парсится: {e}"
            summaries.append(f"• {message.sender}: {Path(path.name).name} — {note}")
            print(f"mail: принят {path.name} от {message.sender} ({note})")
        if message.imap_uid:
            processed_uids.add(message.imap_uid)
            _write_processed_uids(processed_path, processed_uids)

    # Исторические дубликаты тоже считаем просмотренными только после того, как
    # актуальные версии успешно сохранены. Это не даст следующему таймеру снова
    # перебирать всю почтовую историю.
    processed_uids.update(message.imap_uid for message in messages if message.imap_uid)
    _write_processed_uids(processed_path, processed_uids)
    if notify_telegram and token and owner and summaries:
        shown = summaries[:12]
        extra = len(summaries) - len(shown)
        suffix = f"\n… и ещё {extra} файлов" if extra else ""
        send_message(
            token,
            owner,
            f"📬 Обновлены прайсы: {len(summaries)} файлов.\n"
            + "\n".join(shown)
            + suffix
            + "\nОстатки и сроки перед заявкой требуют подтверждения.",
        )


if __name__ == "__main__":
    main()
