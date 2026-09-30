"""MCP-сервер (stdio): ставит задачи в Grok Imagine через драйвер и отдаёт готовые файлы."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field, ValidationError

from .drivers import Driver, DriverError, DriverNotAvailable, build_driver
from .models import (DriverStatus, ImageAspect, ImageModel, ImageRequest, ImageResolution, TaskInfo, TaskList,
                     VideoAspect, VideoModel, VideoRequest, VideoResolution)

log = logging.getLogger("grok_imagine_mcp")

_INSTRUCTIONS = (
    "Инструменты ставят задачи генерации видео и картинок в Grok Imagine (xAI). "
    "Видео готовится асинхронно: grok_generate_video возвращает task_id, готовый файл забирай через "
    "grok_task_status (wait_s — сколько секунд подождать внутри вызова). "
    "Сообщай пользователю о результате только по полям status и output_path; "
    "submitted=False значит, что в xAI ничего не отправлено."
)


def _readable(err: ValidationError) -> str:
    parts = []
    for e in err.errors():
        field = ".".join(str(x) for x in e["loc"]) or "запрос"
        parts.append(f"{field}: {e['msg'].removeprefix('Value error, ')}")
    return "; ".join(parts)


def build_server(driver: Driver) -> MCPServer:
    server = MCPServer("grok-imagine", instructions=_INSTRUCTIONS)

    @server.tool()
    async def grok_generate_video(
        prompt: Annotated[str, Field(description="Описание видео (промт)")],
        image_path: Annotated[str | None, Field(description=(
            "Абсолютный путь к локальному PNG/JPG/WEBP до 10 МБ — первый кадр для режима «фото → видео». "
            "Без него — «текст → видео»."))] = None,
        duration_s: Annotated[int | None, Field(description="Длительность, секунды, от 1 до 15 (по умолчанию решает API)")] = None,
        aspect_ratio: Annotated[VideoAspect | None, Field(description="Формат кадра")] = None,
        resolution: Annotated[VideoResolution | None, Field(description=(
            "Разрешение. Если модель его не поддерживает, API вернёт ошибку"))] = None,
        generate_audio: Annotated[bool | None, Field(description="Делать ли звуковую дорожку (False — без звука)")] = None,
        model: Annotated[VideoModel, Field(description="Модель видео")] = "grok-imagine-video",
    ) -> TaskInfo:
        """Поставить задачу генерации видео. Возвращает task_id сразу; готовое видео забирать через grok_task_status."""
        try:
            req = VideoRequest(prompt=prompt, image_path=image_path, duration_s=duration_s,
                               aspect_ratio=aspect_ratio, resolution=resolution,
                               generate_audio=generate_audio, model=model)
        except ValidationError as e:
            raise ToolError(_readable(e)) from None
        return await _guard(driver.submit_video(req))

    @server.tool()
    async def grok_generate_image(
        prompt: Annotated[str, Field(description="Описание картинки (промт)")],
        aspect_ratio: Annotated[ImageAspect | None, Field(description="Формат кадра")] = None,
        resolution: Annotated[ImageResolution | None, Field(description="1k (~1 Мп) или 2k (~4 Мп)")] = None,
        model: Annotated[ImageModel, Field(description="Модель картинок")] = "grok-imagine-image",
    ) -> TaskInfo:
        """Сгенерировать картинку. Файл сохраняется на диск сразу, путь в output_path."""
        try:
            req = ImageRequest(prompt=prompt, aspect_ratio=aspect_ratio, resolution=resolution, model=model)
        except ValidationError as e:
            raise ToolError(_readable(e)) from None
        return await _guard(driver.submit_image(req))

    @server.tool()
    async def grok_task_status(
        task_id: Annotated[str, Field(description="task_id из grok_generate_video / grok_generate_image")],
        wait_s: Annotated[int, Field(ge=0, le=50, description=(
            "Сколько секунд ждать готовности внутри вызова (0–50). Держите небольшим: у некоторых клиентов "
            "короткий таймаут вызова инструмента"))] = 0,
    ) -> TaskInfo:
        """Состояние задачи. Когда видео готово, оно скачивается на диск, путь в output_path."""
        return await _guard(driver.task_status(task_id, wait_s))

    @server.tool()
    async def grok_list_tasks(
        limit: Annotated[int, Field(ge=1, le=50, description="Сколько последних задач показать")] = 10,
    ) -> TaskList:
        """Последние задачи, новые первыми."""
        return TaskList(tasks=await _guard(driver.list_tasks(limit)))

    @server.tool()
    async def grok_status() -> DriverStatus:
        """Состояние сервера: готов ли он и уходят ли генерации реально в xAI."""
        return await _guard(driver.status())

    return server


async def _guard(coro):
    try:
        return await coro
    except DriverError as e:
        raise ToolError(str(e)) from None


def _state_dir() -> Path:
    # cwd у stdio-сервера задаёт клиент и он непредсказуем, поэтому по умолчанию — домашняя папка
    return Path(os.environ.get("GROK_STATE_DIR") or Path.home() / ".grok_imagine_mcp")


def load_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE построчно; комментарии, пустые и битые строки пропускаются. Нет файла — пустой словарь."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    out = {}
    for line in lines:
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key.isidentifier():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def merged_env(state: Path, environ) -> dict[str, str]:
    """Настройки из <state>/.env (один файл на все клиенты); реальное окружение приоритетнее."""
    return {**load_env_file(state / ".env"), **environ}


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)      # stdout занят протоколом MCP
    state = _state_dir()
    env = merged_env(state, os.environ)
    output = Path(env.get("GROK_OUTPUT_DIR") or state / "output")
    try:
        driver = build_driver(env.get("GROK_DRIVER", "xai"), state, output, env)
    except DriverNotAvailable as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(2) from None
    log.info("grok-imagine-mcp: драйвер %s", driver.name)
    build_server(driver).run()


if __name__ == "__main__":
    main()
