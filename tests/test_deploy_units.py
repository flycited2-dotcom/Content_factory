"""Юниты systemd из deploy/ копируются на сервер как есть (DEPLOY.md §5).

git на Windows не хранит бит исполнения у .sh, поэтому скрипт в ExecStart*/ExecStop*
обязан запускаться через интерпретатор — иначе после копирования юнита сервис
падает на «Permission denied» (так было с update_db_host.sh, см. REPOSITORY-SYNC-2026-10.md).
"""
import re
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
EXEC = re.compile(r"^(Exec\w+)=[-@:+!]*(.+)$", re.M)


def test_shell_scripts_in_units_run_through_interpreter():
    offenders = []
    for unit in sorted(DEPLOY.rglob("*.service")) + sorted(DEPLOY.rglob("*.conf")):
        for key, command in EXEC.findall(unit.read_text(encoding="utf-8")):
            first = command.split()[0] if command.split() else ""
            if first.endswith(".sh"):
                offenders.append(f"{unit.relative_to(DEPLOY)}: {key}={command}")
    assert offenders == []
