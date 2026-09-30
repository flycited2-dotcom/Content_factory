"""Драйверы: xai (реальная генерация через API xAI), dry-run (только запись задачи), unconfigured (нет ключа)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping, Protocol

from .models import DriverStatus, ImageRequest, TaskInfo, VideoRequest
from .store import TaskStore


class DriverError(Exception):
    """Ожидаемый сбой: текст показывается модели и пользователю как есть."""


class DriverNotAvailable(Exception):
    pass


class Driver(Protocol):
    name: str

    async def submit_video(self, req: VideoRequest) -> TaskInfo: ...

    async def submit_image(self, req: ImageRequest) -> TaskInfo: ...

    async def task_status(self, task_id: str, wait_s: int) -> TaskInfo: ...

    async def list_tasks(self, limit: int) -> list[TaskInfo]: ...

    async def status(self) -> DriverStatus: ...


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def describe(task: dict) -> str:
    """Человекочитаемое состояние задачи для ответа модели."""
    st, kind = task["status"], "Видео" if task["kind"] == "video" else "Картинка"
    if st == "recorded_dry_run":
        return "Драйвер xai не подключён (dry-run): задача записана на диск, в xAI НЕ отправлена."
    if st == "pending":
        return "Задача принята xAI и выполняется. Проверьте grok_task_status позже (можно с wait_s=45)."
    if st == "done" and task.get("output_path"):
        return f"{kind} готово: {task['output_path']}"
    if st == "done":
        return (f"{kind} готово, но скачать не удалось: {task.get('error')}. "
                "Повторите grok_task_status: ссылка на результат действует 24 часа.")
    if st == "failed":
        return f"Генерация не удалась: {task.get('error')}"
    if st == "expired":
        return "Запрос истёк на стороне xAI. Запустите генерацию заново."
    return st


def to_info(task: dict) -> TaskInfo:
    return TaskInfo(
        task_id=task["task_id"], kind=task["kind"], driver=task["driver"], status=task["status"],
        submitted=task["submitted"], message=describe(task), created_at=task["created_at"],
        output_path=task.get("output_path"), duration_s=task.get("duration_s"),
        cost_usd=task.get("cost_usd"), error=task.get("error"),
    )


def load_task(store: TaskStore, task_id: str) -> dict:
    try:
        return store.load(task_id)
    except KeyError:
        raise DriverError(f"задача не найдена: {task_id!r}. Список задач: grok_list_tasks") from None


class DryRunDriver:
    """Пишет задачу на диск и НИЧЕГО не отправляет. Проверка связки «клиент → MCP → задача» без ключа и без трат."""

    name = "dry-run"

    def __init__(self, store: TaskStore):
        self.store = store

    def _record(self, kind: str, req: VideoRequest | ImageRequest) -> TaskInfo:
        task = {
            "task_id": self.store.new_id(), "kind": kind, "driver": self.name,
            "status": "recorded_dry_run", "submitted": False, "created_at": now_iso(),
            "request": req.model_dump(mode="json", exclude_none=True),
        }
        self.store.save(task)
        return to_info(task)

    async def submit_video(self, req: VideoRequest) -> TaskInfo:
        return self._record("video", req)

    async def submit_image(self, req: ImageRequest) -> TaskInfo:
        return self._record("image", req)

    async def task_status(self, task_id: str, wait_s: int) -> TaskInfo:
        return to_info(load_task(self.store, task_id))

    async def list_tasks(self, limit: int) -> list[TaskInfo]:
        return [to_info(t) for t in self.store.list(limit)]

    async def status(self) -> DriverStatus:
        return DriverStatus(driver=self.name, ready=True, live=False,
                            detail="Режим dry-run: задачи только записываются на диск. "
                                   "Для реальной генерации уберите GROK_DRIVER=dry-run и задайте XAI_API_KEY.")


class UnconfiguredDriver:
    """Заглушка, если ключа нет: сервер стартует и каждым ответом объясняет, что сделать."""

    name = "unconfigured"

    def __init__(self, reason: str):
        self.reason = reason

    def _fail(self):
        raise DriverError(self.reason)

    async def submit_video(self, req: VideoRequest) -> TaskInfo:
        self._fail()

    async def submit_image(self, req: ImageRequest) -> TaskInfo:
        self._fail()

    async def task_status(self, task_id: str, wait_s: int) -> TaskInfo:
        self._fail()

    async def list_tasks(self, limit: int) -> list[TaskInfo]:
        self._fail()

    async def status(self) -> DriverStatus:
        return DriverStatus(driver=self.name, ready=False, live=False, detail=self.reason)


_NO_KEY = ("XAI_API_KEY не задан. Создайте ключ на console.x.ai (API оплачивается отдельно от подписки SuperGrok) "
           "и передайте его серверу переменной окружения XAI_API_KEY.")


def build_driver(name: str, state_dir, output_dir, env: Mapping[str, str]) -> Driver:
    store = TaskStore(state_dir / "tasks")
    if name == "dry-run":
        return DryRunDriver(store)
    if name == "xai":
        if not env.get("XAI_API_KEY"):
            return UnconfiguredDriver(_NO_KEY)
        from .xai_driver import XaiDriver, make_client
        key = env["XAI_API_KEY"]
        return XaiDriver(lambda: make_client(key), store, output_dir)
    raise DriverNotAvailable(f"Неизвестный драйвер {name!r}. Доступны: xai, dry-run")
