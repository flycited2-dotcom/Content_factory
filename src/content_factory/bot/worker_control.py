"""Wake the existing Excel worker and report its actual systemd state."""
from __future__ import annotations

import shutil
import subprocess
import sys


_START = ("systemctl", "start", "--no-block", "cf-excel.service")
_SHOW = (
    "systemctl", "show", "cf-excel.service", "cf-excel.timer", "--no-pager",
    "--property=Id,LoadState,ActiveState,SubState,Result,ExecMainStatus,NextElapseUSecRealtime",
)
_UNAVAILABLE = "⚠️ Состояние обработчика проверить не удалось. Задачи сохранены; статус: /excel."


def _systemctl_available() -> bool:
    return sys.platform.startswith("linux") and shutil.which("systemctl") is not None


def request_run() -> bool:
    """Request a cycle without restarting an active worker or submitting a job.

    True means systemd accepted the request, not that generation has started.
    The worker retains responsibility for master gates and persisted job IDs.
    """
    if not _systemctl_available():
        return False
    try:
        result = subprocess.run(
            list(_START), capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _units(output: str) -> dict[str, dict[str, str]]:
    units: dict[str, dict[str, str]] = {}
    for block in output.split("\n\n"):
        values = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if values.get("Id") in {"cf-excel.service", "cf-excel.timer"}:
            units[values["Id"]] = values
    return units


def status_lines() -> list[str]:
    """Show running, failed, or waiting state and the real next timer deadline."""
    if not _systemctl_available():
        return [_UNAVAILABLE]
    try:
        result = subprocess.run(
            list(_SHOW), capture_output=True, text=True, timeout=5, check=False,
        )
        if result.returncode != 0:
            return [_UNAVAILABLE]
        units = _units(result.stdout)
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        return [_UNAVAILABLE]

    service = units.get("cf-excel.service", {})
    state = service.get("ActiveState")
    if service.get("LoadState") != "loaded":
        lines = [_UNAVAILABLE]
    elif state in {"active", "activating", "reloading"}:
        lines = ["⚙️ Обработчик сейчас выполняет цикл."]
    elif state == "deactivating":
        lines = ["⚙️ Обработчик завершает цикл."]
    elif state == "failed" or service.get("Result") not in {None, "", "success"}:
        code = service.get("ExecMainStatus", "")
        suffix = f" (код {code})" if code.isdecimal() and code != "0" else ""
        lines = [f"⚠️ Последний запуск обработчика завершился с ошибкой{suffix}. "
                 "Задачи остаются в очереди."]
    elif state == "inactive":
        lines = ["⚙️ Обработчик сейчас ожидает следующего запуска."]
    else:
        lines = [_UNAVAILABLE]

    timer = units.get("cf-excel.timer", {})
    if timer.get("LoadState") != "loaded":
        lines.append("⚠️ Состояние таймера проверить не удалось.")
    elif timer.get("ActiveState") == "active":
        deadline = timer.get("NextElapseUSecRealtime", "")
        if deadline and deadline != "n/a":
            lines.append(f"🕒 Следующая автоматическая проверка: {deadline}.")
        else:
            lines.append("🕒 Время следующей автоматической проверки пока не назначено.")
    else:
        lines.append("⚠️ Таймер обработчика остановлен; автоматическая проверка не назначена.")
    return lines
