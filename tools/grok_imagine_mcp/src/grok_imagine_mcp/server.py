"""MCP-сервер (stdio). Инструменты ставят задачи в Grok Imagine через выбранный драйвер."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field, ValidationError

from .drivers import Driver, DriverNotAvailable, build_driver
from .models import DriverStatus, ImageRequest, Receipt, VideoRequest

log = logging.getLogger("grok_imagine_mcp")

_INSTRUCTIONS = (
    "Инструменты ставят задачи генерации видео и картинок в десктоп-приложение Grok (Imagine). "
    "Проверяй поле submitted_to_app в ответе: False значит, что задача НЕ ушла в приложение, "
    "и сообщать пользователю, что генерация запущена, нельзя."
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
            "Абсолютный путь к локальному PNG/JPG/WEBP — стартовый кадр для режима «фото → видео». "
            "Без него — «текст → видео»."))] = None,
        duration_s: Annotated[int | None, Field(description="Длительность, секунды (если приложение позволяет выбрать)")] = None,
        aspect_ratio: Annotated[str | None, Field(description="Формат кадра, например 9:16 или 16:9")] = None,
    ) -> Receipt:
        """Поставить задачу генерации видео в Grok Imagine. Готовое видео этот инструмент не возвращает."""
        try:
            req = VideoRequest(prompt=prompt, image_path=image_path,
                               duration_s=duration_s, aspect_ratio=aspect_ratio)
        except ValidationError as e:
            raise ToolError(_readable(e)) from None
        return await driver.submit_video(req)

    @server.tool()
    async def grok_generate_image(
        prompt: Annotated[str, Field(description="Описание картинки (промт)")],
        aspect_ratio: Annotated[str | None, Field(description="Формат кадра, например 1:1 или 9:16")] = None,
    ) -> Receipt:
        """Поставить задачу генерации картинки в Grok Imagine."""
        try:
            req = ImageRequest(prompt=prompt, aspect_ratio=aspect_ratio)
        except ValidationError as e:
            raise ToolError(_readable(e)) from None
        return await driver.submit_image(req)

    @server.tool()
    async def grok_status() -> DriverStatus:
        """Состояние драйвера: управляет ли сервер реальным приложением или только пишет задачи в outbox."""
        return await driver.status()

    return server


def _default_outbox() -> Path:
    # cwd у stdio-сервера задаёт клиент и он непредсказуем, поэтому по умолчанию — домашняя папка
    return Path(os.environ.get("GROK_OUTBOX") or Path.home() / ".grok_imagine_mcp" / "outbox")


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)      # stdout занят протоколом MCP
    try:
        driver = build_driver(os.environ.get("GROK_DRIVER", "dry-run"), _default_outbox())
    except DriverNotAvailable as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(2) from None
    log.info("grok-imagine-mcp: драйвер %s", driver.name)
    build_server(driver).run()


if __name__ == "__main__":
    main()
