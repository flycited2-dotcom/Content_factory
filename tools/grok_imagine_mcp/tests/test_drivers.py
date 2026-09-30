import asyncio
import json

import pytest

from grok_imagine_mcp.drivers import (DriverError, DriverNotAvailable, DryRunDriver, UnconfiguredDriver,
                                      build_driver)
from grok_imagine_mcp.models import ImageRequest, VideoRequest
from grok_imagine_mcp.store import TaskStore

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _run(coro):
    return asyncio.run(coro)


def _drv(tmp_path):
    return DryRunDriver(TaskStore(tmp_path / "deep" / "tasks"))


def test_dry_run_records_video_and_does_not_claim_submission(tmp_path):
    drv = _drv(tmp_path)
    info = _run(drv.submit_video(VideoRequest(prompt="закат", duration_s=6, aspect_ratio="9:16")))

    assert info.kind == "video" and info.driver == "dry-run"
    assert info.submitted is False
    assert info.status == "recorded_dry_run"
    assert "НЕ отправлена" in info.message
    saved = json.loads((tmp_path / "deep" / "tasks" / f"{info.task_id}.json").read_text(encoding="utf-8"))
    assert saved["kind"] == "video"
    assert saved["request"]["prompt"] == "закат"
    assert saved["request"]["duration_s"] == 6
    assert saved["request"]["aspect_ratio"] == "9:16"


def test_dry_run_records_image_path_of_photo(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(PNG)
    drv = _drv(tmp_path)
    info = _run(drv.submit_video(VideoRequest(prompt="оживи", image_path=str(img))))
    saved = json.loads((tmp_path / "deep" / "tasks" / f"{info.task_id}.json").read_text(encoding="utf-8"))
    assert saved["request"]["image_path"] == str(img)


def test_dry_run_image_task(tmp_path):
    info = _run(_drv(tmp_path).submit_image(ImageRequest(prompt="кот")))
    assert info.kind == "image" and info.submitted is False


def test_dry_run_task_status_and_list(tmp_path):
    drv = _drv(tmp_path)
    a = _run(drv.submit_image(ImageRequest(prompt="a")))
    got = _run(drv.task_status(a.task_id, 0))
    assert got.task_id == a.task_id and got.status == "recorded_dry_run"
    assert [t.task_id for t in _run(drv.list_tasks(5))] == [a.task_id]


def test_unknown_task_is_driver_error(tmp_path):
    with pytest.raises(DriverError, match="не найдена"):
        _run(_drv(tmp_path).task_status("ffffffffffff", 0))


def test_dry_run_status_says_it_sends_nothing(tmp_path):
    st = _run(_drv(tmp_path).status())
    assert st.driver == "dry-run" and st.live is False and st.ready is True


def test_unconfigured_driver_explains_what_to_do():
    drv = UnconfiguredDriver("XAI_API_KEY не задан")
    st = _run(drv.status())
    assert st.ready is False and st.live is False and "XAI_API_KEY" in st.detail
    with pytest.raises(DriverError, match="XAI_API_KEY"):
        _run(drv.submit_video(VideoRequest(prompt="x")))
    with pytest.raises(DriverError, match="XAI_API_KEY"):
        _run(drv.submit_image(ImageRequest(prompt="x")))
    with pytest.raises(DriverError, match="XAI_API_KEY"):
        _run(drv.task_status("aaaaaaaaaaaa", 0))


def test_build_driver_dry_run(tmp_path):
    assert isinstance(build_driver("dry-run", tmp_path, tmp_path / "out", env={}), DryRunDriver)


def test_build_driver_xai_without_key_is_unconfigured_not_a_crash(tmp_path):
    drv = build_driver("xai", tmp_path, tmp_path / "out", env={})
    assert isinstance(drv, UnconfiguredDriver)


def test_build_driver_xai_with_key(tmp_path):
    from grok_imagine_mcp.xai_driver import XaiDriver
    drv = build_driver("xai", tmp_path, tmp_path / "out", env={"XAI_API_KEY": "test-key"})
    assert isinstance(drv, XaiDriver)


def test_build_driver_unknown_name(tmp_path):
    with pytest.raises(DriverNotAvailable, match="uia"):
        build_driver("uia", tmp_path, tmp_path / "out", env={})
