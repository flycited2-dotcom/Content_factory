import json
import sys
from pathlib import Path

import anyio
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from grok_imagine_mcp.drivers import DryRunDriver, UnconfiguredDriver
from grok_imagine_mcp.server import build_server
from grok_imagine_mcp.store import TaskStore

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
SRC = str(Path(__file__).resolve().parents[1] / "src")


def _with_client(tmp_path, fn, driver=None):
    async def main():
        drv = driver or DryRunDriver(TaskStore(tmp_path / "tasks"))
        async with Client(build_server(drv)) as c:
            return await fn(c)
    return anyio.run(main)


def _payload(res):
    # структурный ответ, если сервер его отдал, иначе JSON из текстового блока
    if res.structured_content is not None:
        return res.structured_content
    return json.loads(res.content[0].text)


def _tasks_dir(tmp_path):
    return tmp_path / "tasks"


def test_lists_five_tools_with_enums_in_schema(tmp_path):
    async def fn(c):
        return {t.name: t for t in (await c.list_tools()).tools}
    tools = _with_client(tmp_path, fn)
    assert set(tools) == {"grok_generate_video", "grok_generate_image", "grok_task_status",
                          "grok_list_tasks", "grok_status"}
    schema = tools["grok_generate_video"].input_schema
    # модель видит допустимые значения прямо в схеме, а не гадает
    assert "16:9" in json.dumps(schema) and "grok-imagine-video-1.5" in json.dumps(schema)


def test_generate_video_roundtrip(tmp_path):
    async def fn(c):
        return await c.call_tool("grok_generate_video",
                                 {"prompt": "закат над морем", "duration_s": 6, "aspect_ratio": "16:9"})
    res = _with_client(tmp_path, fn)
    assert res.is_error is False
    data = _payload(res)
    assert data["kind"] == "video" and data["submitted"] is False
    assert (_tasks_dir(tmp_path) / f"{data['task_id']}.json").exists()


def test_generate_video_from_photo(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(PNG)

    async def fn(c):
        return await c.call_tool("grok_generate_video", {"prompt": "оживи кадр", "image_path": str(img)})
    res = _with_client(tmp_path, fn)
    assert res.is_error is False
    saved = json.loads((_tasks_dir(tmp_path) / f"{_payload(res)['task_id']}.json").read_text(encoding="utf-8"))
    assert saved["request"]["image_path"] == str(img)


def test_generate_image_roundtrip(tmp_path):
    async def fn(c):
        return await c.call_tool("grok_generate_image", {"prompt": "кот в космосе"})
    res = _with_client(tmp_path, fn)
    assert res.is_error is False and _payload(res)["kind"] == "image"


def test_bad_arguments_give_readable_error_and_record_nothing(tmp_path):
    async def fn(c):
        return await c.call_tool("grok_generate_video", {"prompt": "x", "duration_s": 99})
    res = _with_client(tmp_path, fn)
    assert res.is_error is True
    assert "duration_s" in res.content[0].text
    assert not list(_tasks_dir(tmp_path).glob("*.json"))


def test_non_image_file_is_refused(tmp_path):
    fake = tmp_path / "x.png"
    fake.write_bytes(b"password=123")

    async def fn(c):
        return await c.call_tool("grok_generate_video", {"prompt": "x", "image_path": str(fake)})
    res = _with_client(tmp_path, fn)
    assert res.is_error is True and not list(_tasks_dir(tmp_path).glob("*.json"))


def test_task_status_and_list_tools(tmp_path):
    async def fn(c):
        made = await c.call_tool("grok_generate_image", {"prompt": "a"})
        tid = _payload(made)["task_id"]
        st = await c.call_tool("grok_task_status", {"task_id": tid})
        ls = await c.call_tool("grok_list_tasks", {})
        return tid, st, ls
    tid, st, ls = _with_client(tmp_path, fn)
    assert _payload(st)["task_id"] == tid
    assert [t["task_id"] for t in _payload(ls)["tasks"]] == [tid]


def test_task_status_unknown_id_is_readable_error(tmp_path):
    async def fn(c):
        return await c.call_tool("grok_task_status", {"task_id": "../etc/passwd"})
    res = _with_client(tmp_path, fn)
    assert res.is_error is True and "не найдена" in res.content[0].text


def test_wait_s_is_bounded(tmp_path):
    async def fn(c):
        return await c.call_tool("grok_task_status", {"task_id": "aaaaaaaaaaaa", "wait_s": 500})
    res = _with_client(tmp_path, fn)
    assert res.is_error is True and "wait_s" in res.content[0].text


def test_unconfigured_driver_error_reaches_the_model(tmp_path):
    async def fn(c):
        return await c.call_tool("grok_generate_video", {"prompt": "x"})
    res = _with_client(tmp_path, fn, driver=UnconfiguredDriver("XAI_API_KEY не задан"))
    assert res.is_error is True and "XAI_API_KEY" in res.content[0].text


def test_status_tool(tmp_path):
    async def fn(c):
        return await c.call_tool("grok_status", {})
    data = _payload(_with_client(tmp_path, fn))
    assert data["driver"] == "dry-run" and data["live"] is False


def _stdio(tmp_path, **env):
    return StdioServerParameters(
        command=sys.executable, args=["-m", "grok_imagine_mcp"],
        env={"PYTHONPATH": SRC, "GROK_STATE_DIR": str(tmp_path / "state"), **env})


def test_stdio_smoke_dry_run_real_process(tmp_path):
    """Сервер реально запускается процессом и отвечает по stdio (stdout не засорён логами)."""
    async def main():
        async with Client(_stdio(tmp_path, GROK_DRIVER="dry-run")) as c:
            tools = {t.name for t in (await c.list_tools()).tools}
            res = await c.call_tool("grok_generate_image", {"prompt": "смоук"})
            return tools, res

    tools, res = anyio.run(main)
    assert "grok_generate_video" in tools and res.is_error is False
    assert len(list((tmp_path / "state" / "tasks").glob("*.json"))) == 1


def test_stdio_without_api_key_starts_and_explains(tmp_path):
    """Без XAI_API_KEY сервер не падает при старте, а объясняет модели, что сделать."""
    async def main():
        async with Client(_stdio(tmp_path)) as c:
            return (await c.call_tool("grok_status", {}),
                    await c.call_tool("grok_generate_video", {"prompt": "x"}))

    status, gen = anyio.run(main)
    assert _payload(status)["ready"] is False
    assert gen.is_error is True and "XAI_API_KEY" in gen.content[0].text
