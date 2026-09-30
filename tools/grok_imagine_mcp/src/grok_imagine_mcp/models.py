"""Запросы к Grok Imagine (валидируются до любого касания приложения) и квитанции о постановке."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, field_validator

# Страховочные пределы, а не знание о приложении: реальные лимиты Imagine уточним после разведки.
MAX_PROMPT_CHARS = 4000
MAX_DURATION_S = 60
MAX_IMAGE_BYTES = 20 * 1024 * 1024

_ASPECT_RE = re.compile(r"^[1-9]\d?:[1-9]\d?$")


def _looks_like(suffix: str, head: bytes) -> bool:
    """Расширение должно совпадать с содержимым: произвольный файл под видом .png не уходит в приложение."""
    if suffix == ".png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in (".jpg", ".jpeg"):
        return head.startswith(b"\xff\xd8\xff")
    if suffix == ".webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    return False


_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")


def _check_image(value: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"image_path должен быть абсолютным путём: {value}")
    if not path.is_file():
        raise ValueError(f"файл не найден: {value}")
    suffix = path.suffix.lower()
    if suffix not in _IMAGE_SUFFIXES:
        raise ValueError(f"расширение {suffix or '(нет)'} не поддерживается, нужно одно из {', '.join(_IMAGE_SUFFIXES)}")
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError(f"файл слишком велик (больше {MAX_IMAGE_BYTES // (1024 * 1024)} МБ)")
    with path.open("rb") as f:
        head = f.read(16)
    if not _looks_like(suffix, head):
        raise ValueError(f"содержимое не похоже на {suffix}-изображение: {value}")
    return path


class _PromptRequest(BaseModel):
    prompt: str
    aspect_ratio: str | None = None

    @field_validator("prompt")
    @classmethod
    def _prompt(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("prompt пустой")
        if len(v) > MAX_PROMPT_CHARS:
            raise ValueError(f"prompt длиннее {MAX_PROMPT_CHARS} символов")
        return v

    @field_validator("aspect_ratio")
    @classmethod
    def _aspect(cls, v: str | None) -> str | None:
        if v is not None and not _ASPECT_RE.match(v):
            raise ValueError(f"aspect_ratio должен быть вида 16:9, получено {v!r}")
        return v


class VideoRequest(_PromptRequest):
    image_path: Path | None = None
    duration_s: int | None = None

    @field_validator("image_path")
    @classmethod
    def _image(cls, v: Path | None) -> Path | None:
        return None if v is None else _check_image(v)

    @field_validator("duration_s")
    @classmethod
    def _duration(cls, v: int | None) -> int | None:
        if v is not None and not 1 <= v <= MAX_DURATION_S:
            raise ValueError(f"duration_s должен быть от 1 до {MAX_DURATION_S}")
        return v


class ImageRequest(_PromptRequest):
    pass


class Receipt(BaseModel):
    """Результат постановки задачи. submitted_to_app=False означает: в Grok ничего НЕ отправлено."""
    task_id: str
    kind: Literal["video", "image"]
    driver: str
    submitted_to_app: bool
    status: str
    message: str


class DriverStatus(BaseModel):
    driver: str
    ready: bool
    controls_app: bool
    detail: str
