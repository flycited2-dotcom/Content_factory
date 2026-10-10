"""Read owner-editable supplier labels without changing supplier slot keys."""
from __future__ import annotations

import json
from pathlib import Path


def get_source_names(prices_dir: str | Path) -> dict[str, str]:
    """Read ``source_names.json`` on each call; invalid entries are ignored.

    There are no inferred or hardcoded supplier labels. Missing or malformed
    files leave every source available through its original slot identifier.
    UTF-8 BOM is accepted for files saved by Windows editors.
    """
    try:
        value = json.loads((Path(prices_dir) / "source_names.json").read_text(
            encoding="utf-8-sig"
        ))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        slot: label.strip()
        for slot, label in value.items()
        if isinstance(slot, str) and slot and slot == slot.strip()
        and isinstance(label, str) and label.strip()
    }


def source_name(prices_dir: str | Path, slot: str) -> str:
    """Return the configured display label, falling back to the exact slot."""
    return get_source_names(prices_dir).get(slot, slot)
