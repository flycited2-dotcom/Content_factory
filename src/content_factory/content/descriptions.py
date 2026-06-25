"""Готовые тексты описаний на серию (ручные/Codex). manifest.json сопоставляет
series_key → имя файла .txt в той же папке. Текст переопределяет автогенерацию
описания в render_series (живая таблица цен дописывается автоматически)."""
from __future__ import annotations
import json
from pathlib import Path


def load_descriptions(manifest_path: str | Path) -> dict[str, str]:
    """{series_key: текст описания} из manifest.json (key→файл) и .txt-файлов рядом.
    Отсутствующий manifest/файл — не ошибка (просто меньше переопределений)."""
    p = Path(manifest_path)
    if not p.exists():
        return {}
    mapping = json.loads(p.read_text(encoding="utf-8"))
    base = p.parent
    out: dict[str, str] = {}
    for key, fname in mapping.items():
        f = base / fname
        if f.exists():
            text = f.read_text(encoding="utf-8").strip()
            if text:
                out[key] = text
    return out
