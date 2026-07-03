"""Источник «почта»: извлечение .xlsx-вложений из письма."""
from email.message import EmailMessage
from content_factory.ingest.mail_ingest import extract_xlsx_attachments


def _mail(attachments):
    msg = EmailMessage()
    msg["From"] = "info@simfer.com.ru"
    msg["Subject"] = "Прайс из 1С"
    msg.set_content("во вложении")
    for fname, data in attachments:
        msg.add_attachment(data, maintype="application", subtype="octet-stream",
                           filename=fname)
    return bytes(msg)


def test_extracts_xlsx_only():
    raw = _mail([("Прайс_1С.xlsx", b"PK\x03\x04xlsx-data"),
                 ("картинка.png", b"\x89PNG....."),
                 ("старый.xls", b"\xd0\xcf\x11\xe0old")])
    got = extract_xlsx_attachments(raw)
    assert [f for f, _ in got] == ["Прайс_1С.xlsx"]
    assert got[0][1].startswith(b"PK")


def test_no_attachments():
    assert extract_xlsx_attachments(_mail([])) == []
