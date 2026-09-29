"""Fail-closed OCR audit for generated ready-price infographics."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import csv
import io
from pathlib import Path
import re
import shutil
import subprocess


@dataclass(frozen=True)
class OcrWord:
    text: str
    confidence: float


def _words(text: str) -> set[str]:
    return {
        token.casefold().replace("ё", "е")
        for token in re.findall(r"[A-Za-zА-Яа-яЁё]+", text or "")
        if len(token) >= 4
    }


def _distance(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(current[-1] + 1, previous[j] + 1,
                               previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def _matches(word: str, trusted: set[str]) -> bool:
    if word in trusted:
        return True
    # OCR commonly drops one edge letter or confuses one glyph.  A shared long
    # stem also covers normal Russian endings (конфорка/конфорок).
    if len(word) >= 6 and any(len(value) >= 6 and word[:5] == value[:5]
                              for value in trusted):
        return True
    limit = 2 if len(word) >= 6 else 1
    return any(abs(len(word) - len(value)) <= limit and _distance(word, value) <= limit
               for value in trusted)


def find_unverified_terms(card_words: list[OcrWord], original_words: list[OcrWord],
                          *, brand: str, model: str, name: str,
                          features: list[str]) -> list[str]:
    trusted_text = " ".join([brand, model, name, *features, "В наличии"])
    trusted = _words(trusted_text)
    trusted.update(_words(" ".join(w.text for w in original_words if w.confidence >= 70)))
    # Layout labels which carry no product claim.
    trusted.update(_words(
        "модель характеристики класс новый товар артикул "
        "варочная поверхность панель стиральная сушильная машина "
        "холодильник морозильник телевизор"
    ))
    # An electrical adjective is supported by an explicit wattage fact.
    if re.search(r"(?:\d|\s)(?:вт|w)\b", trusted_text, re.IGNORECASE):
        trusted.update(_words("электрический электрическая электрическое"))

    unknown = set()
    for value in card_words:
        if value.confidence < 90:
            continue
        for word in _words(value.text):
            if not _matches(word, trusted):
                unknown.add(word)
    return sorted(unknown)


def find_repeated_feature_terms(card_words: list[OcrWord], features: list[str]) -> list[str]:
    """Reject layouts that print the same feature panel twice.

    Image generators sometimes create a correct right-hand column and then repeat
    the entire specification row along the bottom.  Every word is factually
    allowed, so the ordinary evidence audit cannot see the layout defect.
    """
    expected = Counter(word for value in features for word in _words(value))
    observed = Counter()
    for value in card_words:
        if value.confidence < 70:
            continue
        for word in _words(value.text):
            if word in expected:
                observed[word] += 1
    repeated = sorted(word for word, count in observed.items()
                      if count > expected[word])
    # One repeated label can be a legitimate heading.  A duplicated panel repeats
    # several independent feature labels at once.
    return repeated if len(repeated) >= 3 else []


def _ocr(path: Path, psm: int) -> list[OcrWord]:
    command = shutil.which("tesseract")
    if not command:
        raise RuntimeError("tesseract_not_installed")
    result = subprocess.run(
        [command, str(path), "stdout", "-l", "rus+eng", "--psm", str(psm), "tsv"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=45, check=True,
    )
    words = []
    for row in csv.DictReader(io.StringIO(result.stdout), delimiter="\t"):
        text = (row.get("text") or "").strip()
        try:
            confidence = float(row.get("conf") or -1)
        except ValueError:
            confidence = -1
        if text:
            words.append(OcrWord(text, confidence))
    return words


def audit_card_text(card: Path, original: Path, *, brand: str, model: str,
                    name: str, features: list[str]) -> dict:
    """Reject readable card claims absent from research evidence or source photo."""
    try:
        sparse_words = _ocr(card, 11)
        layout_words = _ocr(card, 6)
        card_words = [*sparse_words, *layout_words]
        original_words = _ocr(original, 11)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        return {"passed": False, "engine": "tesseract-rus+eng",
                "reason": f"ocr_unavailable:{type(exc).__name__}"}
    unknown = find_unverified_terms(
        card_words, original_words, brand=brand, model=model, name=name,
        features=features,
    )
    repeated = find_repeated_feature_terms(layout_words, features)
    reason = (f"unverified_card_text:{','.join(unknown)}" if unknown else
              f"repeated_feature_text:{','.join(repeated)}" if repeated else "")
    return {"passed": not unknown and not repeated, "engine": "tesseract-rus+eng",
            "unverified_terms": unknown, "repeated_feature_terms": repeated,
            **({"reason": reason} if reason else {})}
