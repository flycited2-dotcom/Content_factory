"""Архив сгенерированных карточек по датам — растущая библиотека оригиналов для
переиспользования. Раскладывает PNG-карточки в `archive/ГГГГ-ММ-ДД/` по дате генерации
(берётся из имени файла `..._ГГГГ-ММ-ДД_ЧЧММСС.png`, иначе — fallback/время файла).
Идемпотентно: уже заархивированный файл повторно не копируется."""
from __future__ import annotations
import re
import shutil
from datetime import date, datetime
from pathlib import Path

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})_\d{6}")   # ..._ГГГГ-ММ-ДД_ЧЧММСС
_EXTS = (".png", ".jpg", ".jpeg")


def card_date(filename: str) -> str | None:
    """Дата генерации (ГГГГ-ММ-ДД) из имени файла или None, если не нашли."""
    m = _DATE_RE.search(filename or "")
    return m.group(1) if m else None


def archive_card(src, archive_root, when: date | None = None) -> tuple[Path, bool]:
    """Скопировать карточку в archive_root/<дата>/<имя>. Возвращает (путь, скопировано?).
    Дата: when → из имени файла → дата изменения файла."""
    src = Path(src)
    day = (when.isoformat() if when else None) or card_date(src.name)
    if not day:
        day = datetime.fromtimestamp(src.stat().st_mtime).date().isoformat()
    dest_dir = Path(archive_root) / day
    dest = dest_dir / src.name
    if dest.exists():                       # уже в архиве — не дублируем
        return dest, False
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    return dest, True


def archive_dir(src_dir, archive_root) -> tuple[int, int]:
    """Заархивировать все карточки из src_dir по датам. Возвращает (скопировано, пропущено)."""
    src_dir = Path(src_dir)
    archived = skipped = 0
    from content_factory.archive_s3 import ArchiveError, archive_for
    remote = archive_for(archive_root)
    sources = []
    for f in sorted(src_dir.iterdir()):
        if not (f.is_file() and f.suffix.lower() in _EXTS):
            continue
        sources.append(f)
        if remote is None:
            _, copied = archive_card(f, archive_root)
        else:
            day = card_date(f.name) or datetime.fromtimestamp(f.stat().st_mtime).date().isoformat()
            try:
                copied = remote.put_file(f, day + '/' + f.name)
            except (ArchiveError, OSError):
                # Preserve originals and the previous local archive during an outage.
                # Do not log command stderr or credentials. The copy/verify CLI can
                # drain these retained files before any later explicit prune.
                with remote.local_write():
                    _, copied = archive_card(f, archive_root)
                print('archive: S3 unavailable; retained local fallback')
        if copied:
            archived += 1
        else:
            skipped += 1
    if remote is not None:
        try:
            remote.backup_manifest()
        except (ArchiveError, OSError):
            # Without a fresh remote index, keep a local archive copy of the
            # batch as well. Never delete originals on an upload/backup failure.
            with remote.local_write():
                for source in sources:
                    archive_card(source, archive_root)
            print('archive: manifest backup unavailable; retained local fallback')
    return archived, skipped
