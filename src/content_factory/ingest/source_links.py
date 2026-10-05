"""Keep old price slots available unless their replacement is proven usable.

The registry describes exact old file contents, rather than assuming that all
files with a familiar slot name are obsolete. It never moves or deletes files.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re


_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")


def _read_dict(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def source_refresh_metadata(prices_dir: Path) -> dict:
    """Read refresh status without making a missing/broken report block prices."""
    return _read_dict(Path(prices_dir) / "source_refresh.json")


def _safe_slot(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and value not in {".", ".."}
        and ".." not in value
        and not any(char in value for char in ("/", "\\", ":", "\0"))
        and not any(ord(char) < 32 for char in value)
        and not value.endswith(".")
    )


def _registry(prices_dir: Path) -> list[dict]:
    data = _read_dict(prices_dir / "price_links.json")
    # bool is an int subclass, but true is not a supported schema version.
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        return []
    replacements = data.get("replacements")
    if not isinstance(replacements, list):
        return []
    old_slots = set()
    for entry in replacements:
        if not isinstance(entry, dict):
            return []
        old, new, digest = (entry.get(key) for key in ("old_slot", "new_slot", "old_sha256"))
        if (
            not _safe_slot(old)
            or not _safe_slot(new)
            or old == new
            or old in old_slots
            or not isinstance(digest, str)
            or not _SHA256.fullmatch(digest)
        ):
            return []
        old_slots.add(old)
    return replacements


def _fingerprint(path: Path) -> tuple[int, ...] | None:
    try:
        stat = path.stat()
        if not path.is_file():
            return None
        return (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_dev, stat.st_ino)
    except OSError:
        return None


@lru_cache(maxsize=128)
def _file_digest(path: Path, fingerprint: tuple[int, ...]) -> str | None:
    try:
        if _fingerprint(path) != fingerprint:
            return None
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest() if _fingerprint(path) == fingerprint else None
    except OSError:
        return None


@lru_cache(maxsize=128)
def _usable_price(path: Path, fingerprint: tuple[int, ...]) -> bool:
    # Import lazily: excel_price may call active_source_paths from its loader.
    from content_factory.ingest.excel_price import PriceItem, parse_price_xlsx

    try:
        if _fingerprint(path) != fingerprint:
            return False
        items = parse_price_xlsx(path)
        return (
            bool(items)
            and all(isinstance(item, PriceItem) for item in items)
            and _fingerprint(path) == fingerprint
        )
    except Exception:
        # Bad workbooks must not hide the usable older copy.
        return False


def _slot_path(prices_dir: Path, slot: str) -> Path | None:
    try:
        path = (prices_dir / f"{slot}.xlsx").resolve()
        # Also reject a basename that points outside the directory via symlink.
        return path if path.parent == prices_dir else None
    except (OSError, RuntimeError):
        return None


def active_source_paths(
    prices_dir: Path, paths: list[tuple[str, Path]]
) -> list[tuple[str, Path]]:
    """Exclude only hash-matched copies with readable, nonempty replacements.

    Invalid registries and failed replacement checks leave the original slots
    available. Repeated calls reuse workbook/hash checks until the file changes.
    """
    original = list(paths)
    try:
        prices_dir = Path(prices_dir).resolve()
    except (OSError, RuntimeError):
        return original
    replacements = _registry(prices_dir)
    if not replacements:
        return original
    excluded = set()
    for entry in replacements:
        old = _slot_path(prices_dir, entry["old_slot"])
        new = _slot_path(prices_dir, entry["new_slot"])
        if old is None or new is None:
            continue
        old_state, new_state = _fingerprint(old), _fingerprint(new)
        if old_state is None or new_state is None:
            continue
        if _file_digest(old, old_state) != entry["old_sha256"].lower():
            continue
        if _usable_price(new, new_state):
            excluded.add((entry["old_slot"], old))
    out = []
    for label, path in original:
        try:
            is_excluded = (label, Path(path).resolve()) in excluded
        except (OSError, RuntimeError):
            is_excluded = False
        if not is_excluded:
            out.append((label, path))
    return out
