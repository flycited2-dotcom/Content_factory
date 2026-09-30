import json
import sys
from pathlib import Path

import anyio
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from grok_imagine_mcp.drivers import DryRunDriver
from grok_imagine_mcp.server import build_server

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
SRC = str(Path(__file__).resolve().parents[1] / "src")


def _with_client(tmp_path, fn):
    async def main():
        async with Client(build_server(DryRunDriver(tmp_path / "outbox"))) as c:
            return await fn(c)
    return anyio.run(main)


def _payload(res):
    # структурный ответ, если сервер его отдал, иначе JSON из текстового блока
    if res.structured_content is not None:
        return res.structured_content
    return json.loads(res.content[0].text)


def test_lists_three_tools(tmp_path):
    async def fn(c):
        return {t.name for t in (await c.list_tools()).tools}
    assert _with_client(tmp_path, fn) == {"grok_generate_video", "grok_generate_image", "grok_status"}


def test_generate_video_roundtrip(tmp_path):
    async def fn(c):
        return await c.call_tool("grok_generate_video",
                                 {"prompt": "закат над морем", "duration_s": 6, "aspect_ratio": "16:9"})
    res = _with_client(tmp_path, fn)
    assert res.is_error is False
    data = _payload(res)
    assert data["kind"] == "video"
    assert data["submitted_to_app"] is False
    assert (tmp_path / "outbox" / f"{data['task_id']}.json").exists()


def test_generate_video_from_photo(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(PNG)

    async def fn(c):
        return await c.call_tool("grok_generate_video", {"prompt": "оживи кадр", "image_path": str(img)})
    res = _with_client(tmp_path, fn)
    assert res.is_error is False
    saved = json.loads((tmp_path / "outbox" / f"{_payload(res)['task_id']}.json").read_text(encoding="utf-8"))
    assert saved["request"]["image_path"] == str(img)


def test_generate_image_roundtrip(tmp_path):
    async def fn(c):
        return await c.call_tool("grok_generate_image", {"prompt": "кот в космосе"})
    res = _with_client(tmp_path, fn)
    assert res.is_error is False
    assert _payload(res)["kind"] == "image"


def test_bad_arguments_give_readable_error_and_record_nothing(tmp_path):
    async def fn(c):
        return await c.call_tool("grok_generate_video", {"prompt": "x", "aspect_ratio": "широкий"})
    res = _with_client(tmp_path, fn)
    assert res.is_error is True
    assert "aspect_ratio" in res.content[0].text
    assert not list((tmp_path / "outbox").glob("*.json"))


def test_non_image_file_is_refused(tmp_path):
    fake = tmp_path / "x.png"
    fake.write_bytes(b"password=123")

    async def fn(c):
        return await c.call_tool("grok_generate_video", {"prompt": "x", "image_path": str(fake)})
    res = _with_client(tmp_path, fn)
    assert res.is_error is True
    assert not list((tmp_path / "outbox").glob("*.json"))


def test_status_tool(tmp_path):
    async def fn(c):
        return await c.call_tool("grok_status", {})
    data = _payload(_with_client(tmp_path, fn))
    assert data["driver"] == "dry-run" and data["controls_app"] is False


def test_stdio_smoke_real_process(tmp_path):
    """Сервер реально запускается как процесс и отвечает по stdio (проверка, что stdout не засорён)."""
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "grok_imagine_mcp"],
        env={"PYTHONPATH": SRC, "GROK_OUTBOX": str(tmp_path / "ob")},
    )

    async def main():
        async with Client(params) as c:
            tools = {t.name for t in (await c.list_tools()).tools}
            res = await c.call_tool("grok_generate_image", {"prompt": "смоук"})
            return tools, res

    tools, res = anyio.run(main)
    assert "grok_generate_video" in tools
    assert res.is_error is False
    assert len(list((tmp_path / "ob").glob("*.json"))) == 1
