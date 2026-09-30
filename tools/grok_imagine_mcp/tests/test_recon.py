import json
import sys

import pytest

from grok_imagine_mcp import recon
from grok_imagine_mcp.recon import Probes, classify, filter_candidates, parse_cdp_version, parse_ps_json, run_recon

CDP_BODY = json.dumps({"Browser": "Chrome/130.0.0.0", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/abc"})


def _proc(pid, name="Grok.exe", cmd="", exe=r"C:\Apps\Grok\Grok.exe", ppid=1):
    return {"ProcessId": pid, "ParentProcessId": ppid, "Name": name, "ExecutablePath": exe, "CommandLine": cmd}


def _probes(procs=(), files=(), ports=(), http=None, uia=None):
    calls = {"uia": 0}

    def dump(pid):
        calls["uia"] += 1
        return uia

    p = Probes(
        find_processes=lambda: list(procs),
        list_files=lambda d: list(files),
        listening_ports=lambda pids: list(ports),
        http_get=(lambda url: (http or {}).get(url)),
        dump_uia=dump,
    )
    p.calls = calls
    return p


# ---------- чистые функции ----------

def test_classify_electron_by_asar():
    assert classify(["grok.exe", "resources/app.asar", "ffmpeg.dll"]) == "electron"


def test_classify_electron_unpacked_app_dir_and_case():
    assert classify(["Grok.exe", "Resources/App"]) == "electron"


def test_classify_other_kinds():
    assert classify(["libcef.dll", "app.exe"]) == "cef"
    assert classify(["flutter_windows.dll", "app.exe"]) == "flutter"
    assert classify(["Qt6Core.dll", "app.exe"]) == "qt"
    assert classify(["Microsoft.UI.Xaml.dll", "app.exe"]) == "winui"
    assert classify(["app.exe", "app.dll"]) == "unknown"
    assert classify([]) == "unknown"


def test_parse_ps_json_shapes():
    assert parse_ps_json("") == []
    assert parse_ps_json("  \n") == []
    assert parse_ps_json('{"a": 1}') == [{"a": 1}]          # PowerShell отдаёт объект, если он один
    assert parse_ps_json('[{"a": 1}, {"a": 2}]') == [{"a": 1}, {"a": 2}]
    assert parse_ps_json("не json") == []


def test_parse_cdp_version():
    assert parse_cdp_version(CDP_BODY) == {"browser": "Chrome/130.0.0.0",
                                           "ws": "ws://127.0.0.1:9222/devtools/browser/abc"}
    assert parse_cdp_version(None) is None
    assert parse_cdp_version("<html>") is None
    assert parse_cdp_version('{"x": 1}') is None


def test_filter_candidates_skips_own_tools_even_if_path_contains_hint():
    procs = [
        _proc(10, name="python.exe", exe=r"C:\tools\grok_imagine_mcp\.venv\Scripts\python.exe"),
        _proc(11, name="Grok.exe"),
        _proc(12, name="bash.exe", exe=r"C:\grok-work\bash.exe"),
        _proc(13, name="Grok.exe"),
        _proc(14, name="msedgewebview2.exe", exe=r"C:\WV\msedgewebview2.exe",
              cmd=r'msedgewebview2.exe --user-data-dir="C:\Users\u\AppData\Local\Grok\EBWebView"'),
        _proc(15, name="msedgewebview2.exe", exe=r"C:\WV\msedgewebview2.exe", cmd="--user-data-dir=other"),
        _proc(16, name="notepad.exe", exe=r"C:\Windows\notepad.exe"),
    ]
    got = filter_candidates(procs, "grok", self_pids={13})
    assert sorted(p["ProcessId"] for p in got) == [11, 14]


# ---------- сценарии run_recon ----------

def test_not_running():
    rep = run_recon(_probes(), "grok")
    assert rep["found"] is False
    assert rep["verdict"] == "not-running"
    assert any("запустите" in s.lower() for s in rep["next_steps"])


def test_electron_without_debug_port():
    procs = [_proc(21, cmd="Grok.exe --type=renderer"), _proc(20, cmd='"C:\\Apps\\Grok\\Grok.exe"')]
    rep = run_recon(_probes(procs, files=["grok.exe", "resources/app.asar"]), "grok", want_uia=False)
    assert rep["found"] is True
    assert rep["app"]["kind"] == "electron"
    assert rep["app"]["main_pid"] == 20                      # главный процесс, не renderer
    assert rep["verdict"] == "electron"
    assert rep["cdp"] is None
    assert any("--remote-debugging-port" in s for s in rep["next_steps"])


def test_cdp_ready_skips_non_cdp_ports():
    procs = [_proc(20, cmd='"C:\\Apps\\Grok\\Grok.exe" --remote-debugging-port=9222')]
    http = {"http://127.0.0.1:9222/json/version": CDP_BODY}
    rep = run_recon(_probes(procs, files=["resources/app.asar"], ports=[50001, 9222], http=http), "grok", want_uia=False)
    assert rep["verdict"] == "cdp-ready"
    assert rep["cdp"]["port"] == 9222
    assert rep["cdp"]["browser"] == "Chrome/130.0.0.0"
    assert rep["app"]["debug_flag_in_cmdline"] is True


def test_native_app_gets_uia_dump():
    nodes = [{"depth": 0, "control_type": "Window", "name": "Grok", "automation_id": "", "class_name": "Win"}]
    pr = _probes([_proc(30)], files=["grok.exe", "grok.dll"], uia=nodes)
    rep = run_recon(pr, "grok")
    assert rep["verdict"] == "native"
    assert rep["uia"] == nodes
    assert pr.calls["uia"] == 1


def test_uia_skipped_on_request():
    pr = _probes([_proc(30)], files=["grok.exe"], uia=[{"x": 1}])
    rep = run_recon(pr, "grok", want_uia=False)
    assert pr.calls["uia"] == 0
    assert rep["uia"] is None


def test_webview2_detected_from_child_processes():
    procs = [_proc(40), _proc(41, name="msedgewebview2.exe", exe=r"C:\WV\msedgewebview2.exe",
                              cmd=r'--user-data-dir="C:\Users\u\AppData\Local\Grok\EBWebView"')]
    rep = run_recon(_probes(procs, files=["grok.exe"]), "grok", want_uia=False)
    assert rep["verdict"] == "webview2"
    assert any("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS" in s for s in rep["next_steps"])


def test_unreadable_install_dir_is_reported_not_guessed():
    rep = run_recon(_probes([_proc(50)], files=[]), "grok", want_uia=False)
    assert rep["app"]["install_dir_readable"] is False
    assert rep["app"]["kind"] == "unknown"


def test_report_is_json_serialisable_and_renders():
    rep = run_recon(_probes([_proc(20)], files=["resources/app.asar"]), "grok", want_uia=False)
    json.dumps(rep, ensure_ascii=False)
    text = recon.render_report(rep)
    assert "electron" in text and "--remote-debugging-port" in text


def test_main_refuses_outside_windows(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    assert recon.main(["--out", str(tmp_path / "r.json")]) == 2
    assert "Windows" in capsys.readouterr().err
    assert not (tmp_path / "r.json").exists()


# ---------- слой системы (то, что можно проверить вне Windows) ----------

def test_list_install_files_reads_resources_and_lowercases(tmp_path):
    (tmp_path / "Grok.exe").write_bytes(b"")
    (tmp_path / "Resources").mkdir()
    (tmp_path / "Resources" / "App.asar").write_bytes(b"")
    assert sorted(recon.list_install_files(str(tmp_path))) == ["grok.exe", "resources", "resources/app.asar"]


def test_list_install_files_unreadable_dir_is_empty(tmp_path):
    assert recon.list_install_files(str(tmp_path / "нет")) == []


def test_http_get_local_reads_body_even_with_proxy_env(monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(CDP_BODY.encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")     # прокси из окружения не должен мешать
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    try:
        port = srv.server_address[1]
        assert parse_cdp_version(recon.http_get_local(f"http://127.0.0.1:{port}/json/version")) is not None
    finally:
        srv.shutdown()
    assert recon.http_get_local(f"http://127.0.0.1:{port}/json/version", timeout=0.3) is None   # порт закрыт


def test_http_get_local_refuses_remote_hosts():
    with pytest.raises(ValueError):
        recon.http_get_local("http://example.com/json/version")
