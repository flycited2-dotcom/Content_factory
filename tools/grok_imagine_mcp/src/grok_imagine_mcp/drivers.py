"""Драйверы приложения. Сейчас только dry-run: боевой драйвер пишется после разведки (recon)."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .models import DriverStatus, ImageRequest, Receipt, VideoRequest


class DriverNotAvailable(Exception):
    pass


class Driver(Protocol):
    name: str

    async def submit_video(self, req: VideoRequest) -> Receipt: ...

    async def submit_image(self, req: ImageRequest) -> Receipt: ...

    async def status(self) -> DriverStatus: ...


class DryRunDriver:
    """Пишет задачу в папку outbox и НИЧЕГО не отправляет в приложение.
    Нужен, чтобы проверить связку «клиент → MCP → задача» до появления боевого драйвера."""

    name = "dry-run"

    def __init__(self, outbox: Path):
        self.outbox = Path(outbox)

    def _record(self, kind: str, req: VideoRequest | ImageRequest) -> Receipt:
        task_id = uuid.uuid4().hex[:12]
        self.outbox.mkdir(parents=True, exist_ok=True)
        path = self.outbox / f"{task_id}.json"
        doc = {
            "task_id": task_id,
            "kind": kind,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "request": req.model_dump(mode="json", exclude_none=True),
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return Receipt(
            task_id=task_id, kind=kind, driver=self.name, submitted_to_app=False,
            status="recorded_dry_run",
            message=f"Драйвер приложения не подключён: задача записана в {path}, в Grok НЕ отправлена.",
        )

    async def submit_video(self, req: VideoRequest) -> Receipt:
        return self._record("video", req)

    async def submit_image(self, req: ImageRequest) -> Receipt:
        return self._record("image", req)

    async def status(self) -> DriverStatus:
        return DriverStatus(
            driver=self.name, ready=True, controls_app=False,
            detail="Задачи только записываются в outbox. Чтобы подключить приложение, "
                   "запустите разведку: python -m grok_imagine_mcp.recon",
        )


def build_driver(name: str, outbox: Path) -> Driver:
    if name == "dry-run":
        return DryRunDriver(outbox)
    raise DriverNotAvailable(
        f"Драйвер {name!r} ещё не реализован. Сначала разведка приложения: "
        "python -m grok_imagine_mcp.recon"
    )
