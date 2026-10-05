from types import SimpleNamespace
import subprocess

import pytest

from content_factory.bot import worker_control


def _fake(monkeypatch, *, output="", returncode=0, error=None):
    calls = []
    monkeypatch.setattr(worker_control, "_systemctl_available", lambda: True)

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if error is not None:
            raise error
        return SimpleNamespace(returncode=returncode, stdout=output, stderr="ignored")

    monkeypatch.setattr(worker_control.subprocess, "run", run)
    return calls


def _output(state="inactive", result="success", code="0", timer="active", deadline=None):
    deadline = "Fri 2026-10-02 11:10:00 MSK" if deadline is None else deadline
    return (f"Id=cf-excel.service\nLoadState=loaded\nActiveState={state}\n"
            f"SubState=dead\nResult={result}\nExecMainStatus={code}\n\n"
            f"Id=cf-excel.timer\nLoadState=loaded\nActiveState={timer}\n"
            f"SubState=waiting\nNextElapseUSecRealtime={deadline}\n")


def test_wake_starts_existing_service_without_restart_or_paid_request(monkeypatch):
    calls = _fake(monkeypatch)
    assert worker_control.request_run() is True
    assert calls == [(["systemctl", "start", "--no-block", "cf-excel.service"],
                      {"capture_output": True, "text": True, "timeout": 5, "check": False})]


@pytest.mark.parametrize("error", [OSError("unavailable"),
                                    subprocess.TimeoutExpired("systemctl", 5)])
def test_control_failures_are_soft(monkeypatch, error):
    _fake(monkeypatch, error=error)
    assert worker_control.request_run() is False
    assert "проверить не удалось" in "\n".join(worker_control.status_lines())


def test_systemctl_failure_does_not_echo_stderr_or_claim_work_started(monkeypatch):
    _fake(monkeypatch, returncode=1)
    assert worker_control.request_run() is False
    assert worker_control.status_lines() == [worker_control._UNAVAILABLE]


def test_nonlinux_never_executes_systemctl(monkeypatch):
    monkeypatch.setattr(worker_control.sys, "platform", "win32")
    monkeypatch.setattr(worker_control.subprocess, "run",
                        lambda *a, **kw: pytest.fail("systemctl must not run"))
    assert worker_control.request_run() is False
    assert worker_control.status_lines() == [worker_control._UNAVAILABLE]


def test_linux_without_systemctl_is_soft(monkeypatch):
    monkeypatch.setattr(worker_control.sys, "platform", "linux")
    monkeypatch.setattr(worker_control.shutil, "which", lambda binary: None)
    monkeypatch.setattr(worker_control.subprocess, "run",
                        lambda *a, **kw: pytest.fail("systemctl must not run"))
    assert worker_control.request_run() is False
    assert worker_control.status_lines() == [worker_control._UNAVAILABLE]


def test_failed_cycle_and_actual_next_tick_are_visible(monkeypatch):
    calls = _fake(monkeypatch, output=_output("failed", "exit-code", "1"))
    text = "\n".join(worker_control.status_lines())
    assert "с ошибкой (код 1)" in text
    assert "11:10:00 MSK" in text
    assert "выполняет цикл" not in text
    assert calls[0][0] == list(worker_control._SHOW)
    assert calls[0][1]["timeout"] == 5


@pytest.mark.parametrize("state", ["active", "activating"])
def test_running_state_does_not_report_a_previous_failure(monkeypatch, state):
    _fake(monkeypatch, output=_output(state, "exit-code", "1"))
    text = "\n".join(worker_control.status_lines())
    assert "выполняет цикл" in text
    assert "с ошибкой" not in text


def test_successful_inactive_service_is_waiting(monkeypatch):
    _fake(monkeypatch, output=_output())
    assert "ожидает следующего запуска" in "\n".join(worker_control.status_lines())


def test_stopped_timer_has_no_promised_deadline(monkeypatch):
    _fake(monkeypatch, output=_output(timer="inactive"))
    text = "\n".join(worker_control.status_lines())
    assert "Таймер обработчика остановлен" in text
    assert "11:10:00" not in text


def test_active_timer_without_deadline_does_not_invent_eta(monkeypatch):
    _fake(monkeypatch, output=_output(deadline="n/a"))
    text = "\n".join(worker_control.status_lines())
    assert "пока не назначено" in text
    assert "n/a" not in text


def test_missing_units_do_not_claim_waiting_or_running(monkeypatch):
    _fake(monkeypatch, output="unexpected output")
    text = "\n".join(worker_control.status_lines())
    assert "проверить не удалось" in text
    assert "ожидает следующего запуска" not in text
    assert "выполняет цикл" not in text
