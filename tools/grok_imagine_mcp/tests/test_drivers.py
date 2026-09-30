import asyncio
import json

import pytest

from grok_imagine_mcp.drivers import DriverNotAvailable, DryRunDriver, build_driver
from grok_imagine_mcp.models import ImageRequest, VideoRequest

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _run(coro):
    return asyncio.run(coro)


def test_dry_run_records_video_and_does_not_claim_submission(tmp_path):
    outbox = tmp_path / "deep" / "outbox"          # каталога ещё нет — драйвер создаёт сам
    drv = DryRunDriver(outbox)
    rec = _run(drv.submit_video(VideoRequest(prompt="закат", duration_s=6, aspect_ratio="9:16")))

    assert rec.kind == "video"
    assert rec.driver == "dry-run"
    assert rec.submitted_to_app is False
    assert rec.status == "recorded_dry_run"
    saved = json.loads((outbox / f"{rec.task_id}.json").read_text(encoding="utf-8"))
    assert saved["kind"] == "video"
    assert saved["request"]["prompt"] == "закат"
    assert saved["request"]["duration_s"] == 6
    assert saved["request"]["aspect_ratio"] == "9:16"
    assert saved["created_at"]


def test_dry_run_records_image_request_path(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(PNG)
    drv = DryRunDriver(tmp_path / "out")
    rec = _run(drv.submit_video(VideoRequest(prompt="оживи", image_path=str(img))))
    saved = json.loads((tmp_path / "out" / f"{rec.task_id}.json").read_text(encoding="utf-8"))
    assert saved["request"]["image_path"] == str(img)


def test_dry_run_image_task(tmp_path):
    drv = DryRunDriver(tmp_path)
    rec = _run(drv.submit_image(ImageRequest(prompt="кот")))
    assert rec.kind == "image" and rec.submitted_to_app is False
    assert (tmp_path / f"{rec.task_id}.json").exists()


def test_task_ids_are_unique(tmp_path):
    drv = DryRunDriver(tmp_path)
    ids = {_run(drv.submit_image(ImageRequest(prompt="x"))).task_id for _ in range(20)}
    assert len(ids) == 20


def test_dry_run_status_says_it_does_not_control_app(tmp_path):
    st = _run(DryRunDriver(tmp_path).status())
    assert st.driver == "dry-run"
    assert st.ready is True
    assert st.controls_app is False
    assert "recon" in st.detail


def test_build_driver_dry_run(tmp_path):
    assert isinstance(build_driver("dry-run", tmp_path), DryRunDriver)


def test_build_driver_unknown_points_to_recon(tmp_path):
    with pytest.raises(DriverNotAvailable, match="recon"):
        build_driver("uia", tmp_path)
