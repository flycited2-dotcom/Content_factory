"""Read exact product references from the local archive without creating jobs.

Original product photos are independent of the card template. A finished card
has stricter eligibility: its requested template and text audit must match.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path


@dataclass(frozen=True)
class ArchiveRecovery:
    article: str
    original_path: Path
    card_path: Path | None
    manifest_path: Path
    manifest: dict

    @property
    def evidence(self) -> dict:
        return self.manifest["evidence"]

    @property
    def name(self) -> str:
        return str(self.manifest.get("name") or "")

    @property
    def price(self) -> int | None:
        value = self.manifest.get("price")
        return value if type(value) is int and value > 0 else None


def _identity(value: object) -> str:
    # Deliberately retain punctuation, internal spaces and model suffixes.
    return value.strip().casefold() if isinstance(value, str) else ""


def _basename(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and value not in {".", ".."}
        and ".." not in value
        and not any(char in value for char in ("/", "\\", ":"))
        and not any(ord(char) < 32 for char in value)
        and not value.endswith(".")
    )


def _fingerprint(path: Path) -> tuple[int, ...] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        value = path.stat()
        return (value.st_mtime_ns, value.st_ctime_ns, value.st_size, value.st_dev, value.st_ino)
    except OSError:
        return None


@lru_cache(maxsize=128)
def _valid_png(path: Path, fingerprint: tuple[int, ...]) -> bool:
    from content_factory.drive_archive import valid_png

    try:
        if _fingerprint(path) != fingerprint:
            return False
        return valid_png(path.read_bytes()) and _fingerprint(path) == fingerprint
    except Exception:
        return False


def _image_path(directory: Path, filename: object) -> Path | None:
    if not _basename(filename) or Path(filename).suffix.casefold() != ".png":
        return None
    path = directory / filename
    try:
        if path.is_symlink() or path.resolve().parent != directory:
            return None
        fingerprint = _fingerprint(path)
        return path if fingerprint is not None and _valid_png(path, fingerprint) else None
    except (OSError, RuntimeError):
        return None


def find_archive_recovery(
    content_dir: Path,
    *,
    brand: str,
    model: str,
    card_mode: str,
    article: str | None = None,
    expected_price: int | None = None,
) -> ArchiveRecovery | None:
    """Find one unambiguous, proven exact-model original and optional safe card.

    Brand/model match exactly apart from surrounding spaces and letter case.
    Originals can cross template modes; card reuse additionally requires equal
    card_mode, passed text audit and (when supplied) equal expected_price. The
    caller must check current supplier availability and its own exact name.
    No match, unsafe paths, unsupported manifests and ambiguous matches return
    None. The archive is read only: no files, prices, cache rows or jobs change.
    """
    wanted_brand, wanted_model, wanted_mode = map(_identity, (brand, model, card_mode))
    if not wanted_brand or not wanted_model or not wanted_mode:
        return None
    if article is not None and not _basename(article):
        return None
    if expected_price is not None and (type(expected_price) is not int or expected_price <= 0):
        return None
    try:
        root = Path(content_dir).resolve()
        directories = [root / article] if article is not None else sorted(root.iterdir())
    except (OSError, RuntimeError):
        return None
    matches = []
    for directory in directories:
        try:
            if (directory.is_symlink() or not directory.is_dir()
                    or not _basename(directory.name) or directory.resolve().parent != root):
                continue
            manifest_path = directory / "content.json"
            if manifest_path.is_symlink() or manifest_path.resolve().parent != directory:
                continue
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                continue
            if (type(manifest.get("schema_version")) is not int
                    or manifest["schema_version"] != 1
                    or manifest.get("article") != directory.name
                    or _identity(manifest.get("brand")) != wanted_brand
                    or _identity(manifest.get("model")) != wanted_model):
                continue
            evidence = manifest.get("evidence")
            if not isinstance(evidence, dict) or evidence.get("exact_model") is not True:
                continue
            # If evidence includes an identity, a contradiction must not be hidden
            # by a matching outer manifest.
            if any(key in evidence and _identity(evidence[key]) != value
                   for key, value in (("brand", wanted_brand), ("model", wanted_model))):
                continue
            original = _image_path(directory, manifest.get("original"))
            if original is None:
                continue
            card = None
            audit = manifest.get("card_text_audit")
            if (_identity(manifest.get("card_mode")) == wanted_mode
                    and isinstance(audit, dict) and audit.get("passed") is True
                    and (expected_price is None or (
                        type(manifest.get("price")) is int
                        and manifest["price"] == expected_price))):
                card = _image_path(directory, manifest.get("card"))
            matches.append(ArchiveRecovery(directory.name, original, card, manifest_path, manifest))
            if len(matches) > 1:
                return None
        except (OSError, ValueError, TypeError, KeyError, RecursionError, RuntimeError):
            continue
    return matches[0] if matches else None
