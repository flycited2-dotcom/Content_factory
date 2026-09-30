"""Запросы к Grok Imagine (валидируются до любого обращения к API) и описание задач."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, field_validator

# Допустимые значения — из xai-sdk 1.20.0 (types/model.py, types/video.py, types/image.py).
VideoModel = Literal["grok-imagine-video", "grok-imagine-video-1.5"]
VideoAspect = Literal["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"]
VideoResolution = Literal["480p", "720p", "1080p"]
ImageModel = Literal["grok-imagine-image", "grok-imagine-image-2.0", "grok-imagine-image-quality"]
ImageAspect = Literal["1:1", "3:4", "4:3", "9:16", "16:9", "2:3", "3:2", "9:19.5", "19.5:9",
                      "9:20", "20:9", "1:2", "2:1"]
ImageResolution = Literal["1k", "2k"]

MAX_PROMPT_CHARS = 4000          # страховочный предел, не знание об API
MAX_DURATION_S = 15              # xai-sdk: «Duration of the video to generate in seconds (1-15)»
# Фото уходит base64 (×4/3), а канал xai-sdk режет сообщения на 20 МиБ, поэтому 10 МиБ с запасом.
MAX_IMAGE_BYTES = 10 * 1024 * 1024

_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")


def _looks_like(suffix: str, head: bytes) -> bool:
    """Расширение должно совпадать с содержимым: произвольный файл под видом .png наружу не уходит."""
    if suffix == ".png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in (".jpg", ".jpeg"):
        return head.startswith(b"\xff\xd8\xff")
    if suffix == ".webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    return False


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

    @field_validator("prompt")
    @classmethod
    def _prompt(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("prompt пустой")
        if len(v) > MAX_PROMPT_CHARS:
            raise ValueError(f"prompt длиннее {MAX_PROMPT_CHARS} символов")
        return v


class VideoRequest(_PromptRequest):
    model: VideoModel = "grok-imagine-video"
    image_path: Path | None = None
    duration_s: int | None = None
    aspect_ratio: VideoAspect | None = None
    resolution: VideoResolution | None = None
    generate_audio: bool | None = None

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
    model: ImageModel = "grok-imagine-image"
    aspect_ratio: ImageAspect | None = None
    resolution: ImageResolution | None = None


class TaskInfo(BaseModel):
    """Состояние задачи. submitted=False значит, что в xAI ничего не отправлено (режим dry-run)."""
    task_id: str
    kind: Literal["video", "image"]
    driver: str
    status: str                      # pending | done | failed | expired | recorded_dry_run
    submitted: bool
    message: str
    created_at: str
    output_path: str | None = None
    duration_s: float | None = None
    cost_usd: float | None = None
    error: str | None = None


class TaskList(BaseModel):
    tasks: list[TaskInfo]


class DriverStatus(BaseModel):
    driver: str
    ready: bool
    live: bool                       # True — генерации реально уходят в xAI
    detail: str
