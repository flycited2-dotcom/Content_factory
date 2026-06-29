"""CLI: разложить сгенерированные карточки (выход фотоагента) в архив по датам.
Растущая библиотека оригиналов для переиспользования.

  python -m content_factory.archive_run

Источник — FOTOGEN_OUTPUT_DIR (.env), архив — ARCHIVE_DIR (.env, по умолчанию ./archive).
"""
from __future__ import annotations
from pathlib import Path
from decouple import config
from content_factory.archive import archive_dir


def main():
    src = Path(config("FOTOGEN_OUTPUT_DIR", "/root/ritualb2b/output"))
    root = Path(config("ARCHIVE_DIR", "archive"))
    if not src.is_dir():
        print(f"archive: источник не найден: {src}")
        return
    archived, skipped = archive_dir(src, root)
    print(f"archive: {src} → {root} | новых {archived} | уже было {skipped}")


if __name__ == "__main__":
    main()
