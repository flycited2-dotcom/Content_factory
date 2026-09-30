"""Драйвер xAI: генерация видео и картинок Grok Imagine через официальный xai-sdk (нужен XAI_API_KEY)."""
from __future__ import annotations

import asyncio
import base64
import time
from pathlib import Path
from typing import Awaitable, Callable

import grpc
import httpx
from xai_sdk.proto import deferred_pb2
from xai_sdk.video import VideoResponse

from .drivers import DriverError, load_task, now_iso, to_info
from .models import DriverStatus, ImageRequest, TaskInfo, VideoRequest
from .store import TaskStore

POLL_INTERVAL_S = 2.0
_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def make_client(api_key: str):
    from xai_sdk import AsyncClient
    return AsyncClient(api_key=api_key)


def _explain(e: Exception) -> str:
    if isinstance(e, grpc.aio.AioRpcError):
        code, details = e.code(), e.details() or ""
        if code == grpc.StatusCode.UNAUTHENTICATED:
            return "xAI отклонил ключ: проверьте XAI_API_KEY (console.x.ai)."
        if code in (grpc.StatusCode.PERMISSION_DENIED, grpc.StatusCode.RESOURCE_EXHAUSTED):
            return (f"xAI отказал в запросе ({code.name}): {details}. Возможно, закончились кредиты API "
                    "(они не входят в подписку SuperGrok) или превышен лимит запросов.")
        return f"ошибка xAI API ({code.name}): {details}"
    return f"{type(e).__name__}: {e}"


def _data_url(path: Path) -> str:
    return f"data:{_MIME[path.suffix.lower()]};base64," + base64.b64encode(path.read_bytes()).decode()


def _image_ext(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"                        # xai-sdk отдаёт картинку как JPG


async def download(url: str, dest: Path) -> None:
    if not url.startswith("https://"):
        raise ValueError(f"ожидалась https-ссылка, получено {url[:40]!r}")
    part = dest.with_name(dest.name + ".part")
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=120) as http:
            async with http.stream("GET", url) as r:
                r.raise_for_status()
                with part.open("wb") as f:
                    async for chunk in r.aiter_bytes():
                        f.write(chunk)
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)            # оборванная загрузка не оставляет полфайла


class XaiDriver:
    name = "xai"

    def __init__(self, client_factory: Callable[[], object], store: TaskStore, output_dir: Path, *,
                 download: Callable[[str, Path], Awaitable[None]] = download,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 now: Callable[[], float] = time.monotonic):
        self._factory, self.store, self.output_dir = client_factory, store, Path(output_dir)
        self._download, self._sleep, self._now = download, sleep, now
        self._client_obj = None

    def _client(self):
        # создаём при первом вызове: grpc.aio привязывается к работающему event loop сервера
        if self._client_obj is None:
            self._client_obj = self._factory()
        return self._client_obj

    def _new_task(self, kind: str, req, **extra) -> dict:
        return {"task_id": self.store.new_id(), "kind": kind, "driver": self.name, "submitted": True,
                "created_at": now_iso(), "request": req.model_dump(mode="json", exclude_none=True), **extra}

    async def submit_video(self, req: VideoRequest) -> TaskInfo:
        kw = {k: v for k, v in {"duration": req.duration_s, "aspect_ratio": req.aspect_ratio,
                                "resolution": req.resolution, "generate_audio": req.generate_audio}.items()
              if v is not None}
        if req.image_path:
            kw["image_url"] = _data_url(req.image_path)
        try:
            start = await self._client().video.start(req.prompt, req.model, **kw)
        except Exception as e:           # любой сбой сети/API превращаем в понятный текст
            raise DriverError(_explain(e)) from e
        task = self._new_task("video", req, status="pending", request_id=start.request_id)
        self.store.save(task)
        return to_info(task)

    async def submit_image(self, req: ImageRequest) -> TaskInfo:
        kw = {k: v for k, v in {"aspect_ratio": req.aspect_ratio, "resolution": req.resolution}.items()
              if v is not None}
        try:
            resp = await self._client().image.sample(req.prompt, req.model, image_format="base64", **kw)
            data = await resp.image
        except Exception as e:
            raise DriverError(_explain(e)) from e
        task = self._new_task("image", req, status="done")
        dest = self.output_dir / f"{task['task_id']}{_image_ext(data)}"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        task["output_path"] = str(dest)
        self.store.save(task)
        return to_info(task)

    async def task_status(self, task_id: str, wait_s: int) -> TaskInfo:
        task = load_task(self.store, task_id)
        if task["status"] == "done" and not task.get("output_path") and task["kind"] == "video":
            await self._fetch(task)                       # результат получен, но файл не скачан — повторяем
            return to_info(task)
        if task["status"] != "pending":                   # done, failed, expired и записи dry-run отдаём как есть
            return to_info(task)

        deadline = self._now() + wait_s
        while True:
            try:
                r = await self._client().video.get(task["request_id"])
            except Exception as e:
                raise DriverError(_explain(e)) from e
            if r.status == deferred_pb2.DeferredStatus.DONE:
                await self._finish(task, r)
                break
            if r.status == deferred_pb2.DeferredStatus.EXPIRED:
                task["status"] = "expired"
                break
            if r.status == deferred_pb2.DeferredStatus.FAILED:
                e = r.response.error if r.HasField("response") and r.response.HasField("error") else None
                task.update(status="failed", error=f"[{e.code}] {e.message}" if e else "причина не указана")
                break
            if self._now() >= deadline:                   # всё ещё PENDING (или неизвестный статус)
                break
            await self._sleep(POLL_INTERVAL_S)
        self.store.save(task)
        return to_info(task)

    async def _finish(self, task: dict, r) -> None:
        resp = VideoResponse(r.response)
        try:
            url = resp.url
        except ValueError:                                # пустая ссылка: видео отклонено модерацией
            task.update(status="failed", error="видео не прошло модерацию xAI, ссылка не выдана")
            return
        task.update(status="done", source_url=url, duration_s=float(r.response.video.duration),
                    cost_usd=resp.cost_usd)
        await self._fetch(task)

    async def _fetch(self, task: dict) -> None:
        dest = self.output_dir / f"{task['task_id']}.mp4"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        try:
            await self._download(task["source_url"], dest)
        except Exception as e:
            task["error"] = f"{type(e).__name__}: {e}"
        else:
            task.update(output_path=str(dest), error=None)
        self.store.save(task)

    async def list_tasks(self, limit: int) -> list[TaskInfo]:
        return [to_info(t) for t in self.store.list(limit)]

    async def status(self) -> DriverStatus:
        return DriverStatus(driver=self.name, ready=True, live=True,
                            detail=f"Генерации уходят в xAI (видео и картинки Grok Imagine). Результаты: {self.output_dir}")
