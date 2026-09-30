import asyncio
import base64
import json

import grpc
import pytest
from xai_sdk.proto import deferred_pb2, video_pb2

from grok_imagine_mcp.drivers import DriverError
from grok_imagine_mcp.models import ImageRequest, VideoRequest
from grok_imagine_mcp.store import TaskStore
from grok_imagine_mcp.xai_driver import XaiDriver

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 16
URL = "https://vidgen.example/a.mp4"


# ---------- ответы в настоящих proto-типах xai-sdk ----------

def pending():
    return video_pb2.GetDeferredVideoResponse(status=deferred_pb2.DeferredStatus.PENDING)


def done(url=URL, duration=5, moderation=True):
    return video_pb2.GetDeferredVideoResponse(
        status=deferred_pb2.DeferredStatus.DONE,
        response=video_pb2.VideoResponse(
            video=video_pb2.GeneratedVideo(url=url, duration=duration, respect_moderation=moderation),
            model="grok-imagine-video"))


def failed(code="INTERNAL", message="упало"):
    r = video_pb2.GetDeferredVideoResponse(status=deferred_pb2.DeferredStatus.FAILED)
    r.response.error.code = code
    r.response.error.message = message
    return r


def expired():
    return video_pb2.GetDeferredVideoResponse(status=deferred_pb2.DeferredStatus.EXPIRED)


def rpc_error(code, details):
    return grpc.aio.AioRpcError(code, grpc.aio.Metadata(), grpc.aio.Metadata(), details=details)


# ---------- подставной клиент ----------

class FakeVideo:
    def __init__(self):
        self.start_calls, self.get_calls, self.queue = [], [], [pending()]
        self.start_exc = self.get_exc = None

    async def start(self, prompt, model, **kw):
        if self.start_exc:
            raise self.start_exc
        self.start_calls.append((prompt, model, kw))
        return deferred_pb2.StartDeferredResponse(request_id="req-1")

    async def get(self, request_id):
        if self.get_exc:
            raise self.get_exc
        self.get_calls.append(request_id)
        return self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]


class FakeImageResp:
    def __init__(self, data):
        self._data = data

    @property
    def image(self):                     # как в xai-sdk: свойство, возвращающее корутину
        async def _get():
            return self._data
        return _get()


class FakeImage:
    def __init__(self, data=PNG):
        self.data, self.calls, self.exc = data, [], None

    async def sample(self, prompt, model, **kw):
        if self.exc:
            raise self.exc
        self.calls.append((prompt, model, kw))
        return FakeImageResp(self.data)


class FakeClient:
    def __init__(self):
        self.video, self.image = FakeVideo(), FakeImage()


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    async def sleep(self, s):
        self.t += s


def make(tmp_path, client=None, downloads=None, fail_download=None):
    client = client or FakeClient()
    clock = Clock()
    downloads = downloads if downloads is not None else []

    async def download(url, dest):
        downloads.append((url, dest))
        if fail_download and fail_download():
            raise OSError("сеть пропала")
        dest.write_bytes(b"MP4DATA")

    drv = XaiDriver(lambda: client, TaskStore(tmp_path / "tasks"), tmp_path / "out",
                    download=download, sleep=clock.sleep, now=clock.now)
    return drv, client


def run(coro):
    return asyncio.run(coro)


# ---------- постановка видео ----------

def test_text_to_video_passes_only_given_params(tmp_path):
    drv, c = make(tmp_path)
    info = run(drv.submit_video(VideoRequest(prompt="закат", duration_s=6, aspect_ratio="9:16", resolution="720p")))
    prompt, model, kw = c.video.start_calls[0]
    assert (prompt, model) == ("закат", "grok-imagine-video")
    assert kw == {"duration": 6, "aspect_ratio": "9:16", "resolution": "720p"}     # ничего лишнего, без None
    assert info.kind == "video" and info.driver == "xai"
    assert info.submitted is True and info.status == "pending"
    assert drv.store.load(info.task_id)["request_id"] == "req-1"


def test_generate_audio_false_is_passed(tmp_path):
    drv, c = make(tmp_path)
    run(drv.submit_video(VideoRequest(prompt="x", generate_audio=False, model="grok-imagine-video-1.5")))
    _, model, kw = c.video.start_calls[0]
    assert model == "grok-imagine-video-1.5" and kw == {"generate_audio": False}


@pytest.mark.parametrize("name,data,mime", [("a.png", PNG, "image/png"), ("a.jpg", JPG, "image/jpeg"),
                                            ("a.jpeg", JPG, "image/jpeg"), ("a.webp", WEBP, "image/webp")])
def test_photo_goes_as_base64_data_url(tmp_path, name, data, mime):
    p = tmp_path / name
    p.write_bytes(data)
    drv, c = make(tmp_path)
    run(drv.submit_video(VideoRequest(prompt="оживи", image_path=str(p))))
    kw = c.video.start_calls[0][2]
    assert kw["image_url"] == f"data:{mime};base64," + base64.b64encode(data).decode()


def test_task_record_does_not_contain_image_bytes(tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(PNG)
    drv, _ = make(tmp_path)
    info = run(drv.submit_video(VideoRequest(prompt="x", image_path=str(p))))
    raw = (tmp_path / "tasks" / f"{info.task_id}.json").read_text(encoding="utf-8")
    assert "base64" not in raw and str(p) in raw


def test_start_auth_error_is_readable_and_records_nothing(tmp_path):
    drv, c = make(tmp_path)
    c.video.start_exc = rpc_error(grpc.StatusCode.UNAUTHENTICATED, "bad key")
    with pytest.raises(DriverError, match="XAI_API_KEY"):
        run(drv.submit_video(VideoRequest(prompt="x")))
    assert run(drv.list_tasks(10)) == []


def test_start_out_of_credits_is_readable(tmp_path):
    drv, c = make(tmp_path)
    c.video.start_exc = rpc_error(grpc.StatusCode.RESOURCE_EXHAUSTED, "no credits")
    with pytest.raises(DriverError, match="(?i)кредит"):
        run(drv.submit_video(VideoRequest(prompt="x")))


def test_start_other_error_carries_code_and_details(tmp_path):
    drv, c = make(tmp_path)
    c.video.start_exc = rpc_error(grpc.StatusCode.INVALID_ARGUMENT, "unsupported resolution")
    with pytest.raises(DriverError, match="INVALID_ARGUMENT.*unsupported resolution"):
        run(drv.submit_video(VideoRequest(prompt="x", resolution="1080p")))


# ---------- статус и скачивание ----------

def _submitted(tmp_path, **kw):
    drv, c = make(tmp_path, **kw)
    info = run(drv.submit_video(VideoRequest(prompt="x")))
    return drv, c, info.task_id


def test_pending_stays_pending(tmp_path):
    drv, c, tid = _submitted(tmp_path)
    info = run(drv.task_status(tid, 0))
    assert info.status == "pending" and info.output_path is None
    assert len(c.video.get_calls) == 1 and c.video.get_calls[0] == "req-1"


def test_done_downloads_to_output_dir(tmp_path):
    dl = []
    drv, c, tid = _submitted(tmp_path, downloads=dl)
    c.video.queue = [done(duration=7)]
    info = run(drv.task_status(tid, 0))
    assert info.status == "done"
    assert info.output_path == str(tmp_path / "out" / f"{tid}.mp4")
    assert (tmp_path / "out" / f"{tid}.mp4").read_bytes() == b"MP4DATA"
    assert info.duration_s == 7
    assert dl == [(URL, tmp_path / "out" / f"{tid}.mp4")]
    assert drv.store.load(tid)["status"] == "done"


def test_done_is_final_and_not_redownloaded(tmp_path):
    dl = []
    drv, c, tid = _submitted(tmp_path, downloads=dl)
    c.video.queue = [done()]
    run(drv.task_status(tid, 0))
    again = run(drv.task_status(tid, 0))
    assert again.status == "done" and len(dl) == 1 and len(c.video.get_calls) == 1


def test_wait_polls_until_done(tmp_path):
    drv, c, tid = _submitted(tmp_path)
    c.video.queue = [pending(), pending(), done()]
    info = run(drv.task_status(tid, 30))
    assert info.status == "done" and len(c.video.get_calls) == 3


def test_wait_gives_up_and_stays_pending(tmp_path):
    drv, c, tid = _submitted(tmp_path)
    c.video.queue = [pending()]
    info = run(drv.task_status(tid, 10))
    assert info.status == "pending"
    assert 2 <= len(c.video.get_calls) <= 12          # опрашивал несколько раз, но не бесконечно


def test_failed_reports_code_and_message(tmp_path):
    drv, c, tid = _submitted(tmp_path)
    c.video.queue = [failed("CONTENT_POLICY", "нарушает правила")]
    info = run(drv.task_status(tid, 0))
    assert info.status == "failed"
    assert "CONTENT_POLICY" in info.error and "нарушает правила" in info.error


def test_failed_is_final(tmp_path):
    drv, c, tid = _submitted(tmp_path)
    c.video.queue = [failed()]
    run(drv.task_status(tid, 0))
    run(drv.task_status(tid, 0))
    assert len(c.video.get_calls) == 1


def test_expired(tmp_path):
    drv, c, tid = _submitted(tmp_path)
    c.video.queue = [expired()]
    info = run(drv.task_status(tid, 0))
    assert info.status == "expired" and "заново" in info.message


def test_moderation_block_is_failed_not_a_crash(tmp_path):
    drv, c, tid = _submitted(tmp_path)
    c.video.queue = [done(url="", moderation=False)]
    info = run(drv.task_status(tid, 0))
    assert info.status == "failed" and "модерац" in info.error.lower()


def test_download_failure_keeps_task_done_and_retries_next_time(tmp_path):
    attempts = {"n": 0}

    def fail():
        attempts["n"] += 1
        return attempts["n"] == 1                       # первая попытка падает, вторая проходит

    drv, c, tid = _submitted(tmp_path, fail_download=fail)
    c.video.queue = [done()]
    first = run(drv.task_status(tid, 0))
    assert first.status == "done" and first.output_path is None
    assert "скачать не удалось" in first.message and "24" in first.message
    second = run(drv.task_status(tid, 0))
    assert second.output_path == str(tmp_path / "out" / f"{tid}.mp4")
    assert (tmp_path / "out" / f"{tid}.mp4").exists()


def test_get_error_is_readable(tmp_path):
    drv, c, tid = _submitted(tmp_path)
    c.video.get_exc = rpc_error(grpc.StatusCode.UNAVAILABLE, "try later")
    with pytest.raises(DriverError, match="UNAVAILABLE"):
        run(drv.task_status(tid, 0))
    assert drv.store.load(tid)["status"] == "pending"      # временный сбой не хоронит задачу


# ---------- картинки ----------

def test_image_is_generated_and_saved_with_real_extension(tmp_path):
    drv, c = make(tmp_path)
    info = run(drv.submit_image(ImageRequest(prompt="кот", aspect_ratio="1:1", resolution="2k")))
    prompt, model, kw = c.image.calls[0]
    assert (prompt, model) == ("кот", "grok-imagine-image")
    assert kw == {"image_format": "base64", "aspect_ratio": "1:1", "resolution": "2k"}
    assert info.kind == "image" and info.status == "done" and info.submitted is True
    assert info.output_path == str(tmp_path / "out" / f"{info.task_id}.png")
    assert (tmp_path / "out" / f"{info.task_id}.png").read_bytes() == PNG


@pytest.mark.parametrize("data,ext", [(JPG, ".jpg"), (WEBP, ".webp"), (b"unknown", ".jpg")])
def test_image_extension_follows_content(tmp_path, data, ext):
    drv, _ = make(tmp_path, client=_client_with_image(data))
    info = run(drv.submit_image(ImageRequest(prompt="x")))
    assert info.output_path.endswith(ext)


def _client_with_image(data):
    c = FakeClient()
    c.image = FakeImage(data)
    return c


def test_image_error_is_readable_and_records_nothing(tmp_path):
    drv, c = make(tmp_path)
    c.image.exc = rpc_error(grpc.StatusCode.UNAUTHENTICATED, "bad key")
    with pytest.raises(DriverError, match="XAI_API_KEY"):
        run(drv.submit_image(ImageRequest(prompt="x")))
    assert run(drv.list_tasks(10)) == []


def test_image_task_status_is_just_the_record(tmp_path):
    drv, c = make(tmp_path)
    info = run(drv.submit_image(ImageRequest(prompt="x")))
    assert run(drv.task_status(info.task_id, 0)).output_path == info.output_path


# ---------- общее ----------

def test_list_tasks_newest_first(tmp_path):
    drv, _ = make(tmp_path)
    a = run(drv.submit_video(VideoRequest(prompt="a")))
    b = run(drv.submit_image(ImageRequest(prompt="b")))
    ids = [t.task_id for t in run(drv.list_tasks(10))]
    assert set(ids) == {a.task_id, b.task_id}


def test_status_reports_live(tmp_path):
    drv, _ = make(tmp_path)
    st = run(drv.status())
    assert st.driver == "xai" and st.live is True and st.ready is True
    assert "test-key" not in json.dumps(st.model_dump())


def test_client_is_created_lazily_once(tmp_path):
    made = []

    def factory():
        made.append(1)
        return FakeClient()

    drv = XaiDriver(factory, TaskStore(tmp_path / "t"), tmp_path / "o")
    assert made == []                        # при старте сервера клиент не создаётся (нужен работающий event loop)
    run(drv.submit_image(ImageRequest(prompt="x")))
    run(drv.submit_image(ImageRequest(prompt="y")))
    assert made == [1]


# ---------- реальная загрузка файла (httpx с подставным транспортом) ----------

def _patch_http(monkeypatch, handler):
    import httpx
    from grok_imagine_mcp import xai_driver
    real = httpx.AsyncClient
    monkeypatch.setattr(xai_driver.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_download_streams_to_file_without_leftovers(tmp_path, monkeypatch):
    import httpx
    from grok_imagine_mcp.xai_driver import download
    _patch_http(monkeypatch, lambda req: httpx.Response(200, content=b"V" * 5000))
    dest = tmp_path / "v.mp4"
    run(download("https://vidgen.example/a.mp4", dest))
    assert dest.read_bytes() == b"V" * 5000
    assert [p.name for p in tmp_path.iterdir()] == ["v.mp4"]


def test_download_http_error_leaves_no_files(tmp_path, monkeypatch):
    import httpx
    from grok_imagine_mcp.xai_driver import download
    _patch_http(monkeypatch, lambda req: httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        run(download("https://vidgen.example/a.mp4", tmp_path / "v.mp4"))
    assert list(tmp_path.iterdir()) == []


def test_download_refuses_non_https(tmp_path):
    from grok_imagine_mcp.xai_driver import download
    with pytest.raises(ValueError, match="https"):
        run(download("http://vidgen.example/a.mp4", tmp_path / "v.mp4"))
    with pytest.raises(ValueError, match="https"):
        run(download("file:///etc/passwd", tmp_path / "v.mp4"))


# ---------- совместимость с настоящим xai-sdk (офлайн, без сети и ключа) ----------

def test_calls_match_real_sdk_signatures():
    import inspect
    from grok_imagine_mcp.xai_driver import make_client

    async def main():
        c = make_client("test-key-not-real")
        inspect.signature(c.video.start).bind(
            "p", "grok-imagine-video", duration=5, aspect_ratio="16:9", resolution="720p",
            generate_audio=False, image_url="data:image/png;base64,AA==")
        inspect.signature(c.video.get).bind("request-id")
        inspect.signature(c.image.sample).bind(
            "p", "grok-imagine-image", image_format="base64", aspect_ratio="1:1", resolution="2k")
    asyncio.run(main())


def test_record_from_dry_run_is_returned_as_is_not_polled(tmp_path):
    # переключились с dry-run на xai в той же папке состояния: старая задача не должна ронять опрос
    drv, c = make(tmp_path)
    drv.store.save({"task_id": "aaaaaaaaaaaa", "kind": "video", "driver": "dry-run", "status": "recorded_dry_run",
                    "submitted": False, "created_at": "2026-09-30T10:00:00+00:00", "request": {"prompt": "x"}})
    info = run(drv.task_status("aaaaaaaaaaaa", 5))
    assert info.status == "recorded_dry_run" and info.submitted is False
    assert c.video.get_calls == []
