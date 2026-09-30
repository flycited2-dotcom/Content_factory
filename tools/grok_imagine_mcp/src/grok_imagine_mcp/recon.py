"""Разведка десктоп-приложения Grok на Windows (только чтение, ничего не запускает и не меняет).

Отвечает на вопрос «чем управлять»: Electron/WebView2 (тогда CDP по порту отладки) или нативное окно
(тогда UI Automation). Результат — recon-report.json, по нему пишется боевой драйвер.

Запуск:  python -m grok_imagine_mcp.recon          (Grok должен быть запущен)
"""
from __future__ import annotations

import argparse
import http.client
import json
import ntpath
import os
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# Свои инструменты и оболочки: путь venv или папки с «grok» в имени не должен делать их кандидатами.
_SELF_TOOLS = {
    "python.exe", "pythonw.exe", "py.exe", "powershell.exe", "pwsh.exe", "cmd.exe", "bash.exe", "sh.exe",
    "node.exe", "conhost.exe", "windowsterminal.exe", "claude.exe", "codex.exe",
}
_WEBVIEW2 = "msedgewebview2.exe"
_DEBUG_FLAG = re.compile(r"--remote-debugging-port=(\d+)")
_CMDLINE_LIMIT = 400


@dataclass
class Probes:
    """Всё, что касается реальной системы, — через эти функции, чтобы логика тестировалась без Windows."""
    find_processes: Callable[[], list[dict]]
    list_files: Callable[[str], list[str]]
    listening_ports: Callable[[list[int]], list[int]]
    http_get: Callable[[str], str | None]
    dump_uia: Callable[[int], list[dict] | None]


# ---------- чистая логика ----------

def parse_ps_json(text: str) -> list:
    """ConvertTo-Json отдаёт объект, если он один, массив, если несколько, и пусто, если нет ничего."""
    text = (text or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else [data]


def parse_cdp_version(body: str | None) -> dict | None:
    if not body:
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "webSocketDebuggerUrl" not in data:
        return None
    return {"browser": data.get("Browser", ""), "ws": data["webSocketDebuggerUrl"]}


def classify(files: list[str]) -> str:
    """Эвристика по файлам каталога установки. Неуверенное — 'unknown', не угадываем."""
    names = {f.lower().replace("\\", "/") for f in files}
    if "resources/app.asar" in names or "resources/app" in names or "electron.exe" in names:
        return "electron"
    if "libcef.dll" in names:
        return "cef"
    if "flutter_windows.dll" in names:
        return "flutter"
    if "qt5core.dll" in names or "qt6core.dll" in names:
        return "qt"
    if "microsoft.ui.xaml.dll" in names:
        return "winui"
    return "unknown"


def filter_candidates(procs: list[dict], hint: str, self_pids: set[int]) -> list[dict]:
    hint = hint.lower()
    out = []
    for p in procs:
        name = (p.get("Name") or "").lower()
        if p.get("ProcessId") in self_pids or name in _SELF_TOOLS:
            continue
        exe = (p.get("ExecutablePath") or "").lower()
        cmd = (p.get("CommandLine") or "").lower()
        # дочерние процессы WebView2 называются одинаково у всех приложений — берём только «наши» по user-data-dir
        if hint in name or hint in exe or (name == _WEBVIEW2 and hint in cmd):
            out.append(p)
    return out


def _is_webview(p: dict) -> bool:
    return (p.get("Name") or "").lower() == _WEBVIEW2


def _pick_main(cands: list[dict]) -> dict:
    app = [p for p in cands if not _is_webview(p)] or cands
    top = [p for p in app if "--type=" not in (p.get("CommandLine") or "")] or app
    return min(top, key=lambda p: p.get("ProcessId") or 0)


def run_recon(probes: Probes, hint: str = "grok", want_uia: bool = True) -> dict:
    cands = filter_candidates(probes.find_processes(), hint, {os.getpid(), os.getppid()})
    report: dict = {"hint": hint, "found": bool(cands), "processes": [], "app": None,
                    "cdp": None, "uia": None, "verdict": "not-running", "next_steps": []}
    if not cands:
        report["next_steps"].append(
            f"Процессы с «{hint}» не найдены. Запустите приложение Grok, откройте окно Imagine и повторите разведку.")
        return report

    main = _pick_main(cands)
    exe = main.get("ExecutablePath")
    install_dir = ntpath.dirname(exe) if exe else None
    files = probes.list_files(install_dir) if install_dir else []
    kind = classify(files)
    webview_children = sum(1 for p in cands if _is_webview(p))
    cmdline = main.get("CommandLine") or ""
    flag = _DEBUG_FLAG.search(cmdline)

    report["processes"] = [
        {"pid": p.get("ProcessId"), "ppid": p.get("ParentProcessId"), "name": p.get("Name"),
         "exe": p.get("ExecutablePath"), "cmdline": (p.get("CommandLine") or "")[:_CMDLINE_LIMIT]}
        for p in cands[:30]
    ]
    report["app"] = {
        "main_pid": main.get("ProcessId"), "exe": exe, "install_dir": install_dir,
        "install_dir_readable": bool(files), "kind": kind,
        "debug_flag_in_cmdline": bool(flag), "webview2_children": webview_children,
    }

    ports = probes.listening_ports([p["ProcessId"] for p in cands if p.get("ProcessId") is not None])
    candidates = [int(flag.group(1))] if flag else []
    candidates += [p for p in ports if p not in candidates]
    for port in candidates:
        info = parse_cdp_version(probes.http_get(f"http://127.0.0.1:{port}/json/version"))
        if info:
            report["cdp"] = {"port": port, "browser": info["browser"]}
            break

    if report["cdp"]:
        report["verdict"] = "cdp-ready"
    elif kind == "electron":
        report["verdict"] = "electron"
    elif webview_children:
        report["verdict"] = "webview2"
    elif kind == "unknown" and not files:
        report["verdict"] = "unknown"
    else:
        report["verdict"] = "native"

    if want_uia and report["verdict"] != "cdp-ready":
        report["uia"] = probes.dump_uia(main["ProcessId"])

    report["next_steps"] = _next_steps(report)
    return report


def _next_steps(rep: dict) -> list[str]:
    v, exe = rep["verdict"], (rep["app"] or {}).get("exe") or r"<путь к Grok.exe>"
    if v == "cdp-ready":
        return [f"Порт отладки {rep['cdp']['port']} отвечает — драйвер будет управлять приложением через CDP. "
                "Пришлите recon-report.json."]
    if v == "electron":
        return [
            "Приложение на Electron, порт отладки не найден. Полностью закройте Grok (и в трее) и запустите в PowerShell: "
            f'& "{exe}" --remote-debugging-port=9222',
            "Затем снова запустите разведку. Если флаг игнорируется, останется путь через UIA (раздел uia в отчёте).",
        ]
    if v == "webview2":
        return [
            "Окно приложения — WebView2. Полностью закройте Grok и запустите в PowerShell: "
            f'$env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS="--remote-debugging-port=9222"; & "{exe}"',
            "Затем снова запустите разведку.",
        ]
    if v == "unknown":
        return ["Каталог установки не читается (так бывает у приложений из Microsoft Store), тип не определён. "
                "Пришлите отчёт как есть, определим по процессам."]
    steps = ["Приложение не на Chromium или недоступно для отладки: будем управлять через UI Automation."]
    if rep.get("uia") is None:
        steps.append("UIA-дамп не получен. Установите pywinauto (pip install pywinauto) и повторите.")
    return steps


def render_report(rep: dict) -> str:
    lines = [f"Вердикт: {rep['verdict']}"]
    app = rep.get("app")
    if app:
        lines.append(f"Тип приложения (эвристика): {app['kind']}; exe: {app['exe']}")
        if not app["install_dir_readable"]:
            lines.append("Каталог установки не прочитан.")
    if rep.get("cdp"):
        lines.append(f"CDP: порт {rep['cdp']['port']} ({rep['cdp']['browser']})")
    if rep.get("uia") is not None:
        lines.append(f"UIA: элементов в дампе — {len(rep['uia'])}")
    lines.append("Дальше:")
    lines += [f"  - {s}" for s in rep["next_steps"]]
    return "\n".join(lines)


# ---------- реальная система (Windows) ----------

def _powershell(script: str, timeout: int = 30) -> str:
    full = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + script
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", full],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout if r.returncode == 0 else ""


def _find_processes() -> list[dict]:
    return parse_ps_json(_powershell(
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine | ConvertTo-Json -Compress"))


def _listening_ports(pids: list[int]) -> list[int]:
    ids = ",".join(str(int(p)) for p in pids)
    out = _powershell(
        f"$p=@({ids}); Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | "
        "Where-Object { $p -contains $_.OwningProcess } | "
        "Select-Object -ExpandProperty LocalPort | ConvertTo-Json -Compress")
    return sorted({int(x) for x in parse_ps_json(out) if isinstance(x, int)})


def list_install_files(install_dir: str) -> list[str]:
    """Имена верхнего уровня и содержимое resources/ в нижнем регистре; пусто, если каталог не читается."""
    out: list[str] = []
    try:
        for e in os.scandir(install_dir):
            out.append(e.name.lower())
            if e.name.lower() == "resources" and e.is_dir():
                out += [f"resources/{r.name.lower()}" for r in os.scandir(e.path)]
    except OSError:
        return []
    return out


_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_get_local(url: str, timeout: float = 1.5) -> str | None:
    if not url.startswith("http://127.0.0.1:"):
        raise ValueError(f"только localhost: {url}")
    try:
        with _NO_PROXY_OPENER.open(url, timeout=timeout) as r:    # прокси из окружения для localhost не нужен
            return r.read(65536).decode("utf-8", "replace")
    except (OSError, http.client.HTTPException):
        return None


def dump_uia_tree(pid: int, max_depth: int = 10, max_nodes: int = 400, name_len: int = 60) -> list[dict] | None:
    try:
        from pywinauto import Desktop
    except ImportError:
        return None
    nodes: list[dict] = []

    def walk(el, depth: int) -> None:
        if len(nodes) >= max_nodes or depth > max_depth:
            return
        info = el.element_info
        nodes.append({"depth": depth, "control_type": info.control_type or "",
                      "name": (info.name or "")[:name_len],
                      "automation_id": info.automation_id or "", "class_name": info.class_name or ""})
        for child in el.children():
            walk(child, depth + 1)

    try:
        for window in Desktop(backend="uia").windows(process=pid):
            walk(window, 0)
    except Exception as e:        # разведка best-effort: отчёт нужен, даже если UIA споткнулся
        print(f"UIA-дамп не удался: {type(e).__name__}: {e}", file=sys.stderr)
        return None
    return nodes


def windows_probes() -> Probes:
    return Probes(find_processes=_find_processes, list_files=list_install_files,
                  listening_ports=_listening_ports, http_get=http_get_local, dump_uia=dump_uia_tree)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="grok_imagine_mcp.recon", description=__doc__.splitlines()[0])
    ap.add_argument("--hint", default="grok", help="подстрока имени/пути процесса приложения (по умолчанию grok)")
    ap.add_argument("--no-uia", action="store_true", help="не снимать дамп интерфейса через UI Automation")
    ap.add_argument("--out", default="recon-report.json", help="куда сохранить отчёт")
    args = ap.parse_args(argv)
    if sys.platform != "win32":
        print("Разведка рассчитана на Windows: запустите её на машине, где установлено приложение Grok.",
              file=sys.stderr)
        return 2
    report = run_recon(windows_probes(), args.hint, want_uia=not args.no_uia)
    out = Path(args.out)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(render_report(report))
    print(f"\nОтчёт: {out.resolve()}\nПрочитайте его перед отправкой: там пути на диске и подписи элементов интерфейса.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
