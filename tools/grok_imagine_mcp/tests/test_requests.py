import pytest
from pydantic import ValidationError

from grok_imagine_mcp import models
from grok_imagine_mcp.models import ImageRequest, VideoRequest

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 16


def _img(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_prompt_is_stripped():
    assert VideoRequest(prompt="  закат над морем \n").prompt == "закат над морем"


@pytest.mark.parametrize("bad", ["", "   ", "\n\t"])
def test_empty_prompt_rejected(bad):
    with pytest.raises(ValidationError):
        VideoRequest(prompt=bad)


def test_too_long_prompt_rejected():
    with pytest.raises(ValidationError):
        ImageRequest(prompt="а" * (models.MAX_PROMPT_CHARS + 1))


@pytest.mark.parametrize("ok", ["9:16", "16:9", "1:1", "21:9"])
def test_aspect_ratio_ok(ok):
    assert VideoRequest(prompt="x", aspect_ratio=ok).aspect_ratio == ok


@pytest.mark.parametrize("bad", ["9-16", "abc", "16:", "0:0", "100:1", "16 : 9"])
def test_aspect_ratio_rejected(bad):
    with pytest.raises(ValidationError):
        ImageRequest(prompt="x", aspect_ratio=bad)


@pytest.mark.parametrize("bad", [0, -1, models.MAX_DURATION_S + 1])
def test_duration_out_of_range_rejected(bad):
    with pytest.raises(ValidationError):
        VideoRequest(prompt="x", duration_s=bad)


def test_duration_ok():
    assert VideoRequest(prompt="x", duration_s=6).duration_s == 6


@pytest.mark.parametrize("name,data", [("a.png", PNG), ("a.jpg", JPG), ("a.jpeg", JPG), ("a.webp", WEBP)])
def test_image_formats_accepted(tmp_path, name, data):
    p = _img(tmp_path, name, data)
    assert VideoRequest(prompt="x", image_path=str(p)).image_path == p


def test_image_relative_path_rejected():
    with pytest.raises(ValidationError, match="абсолютн"):
        VideoRequest(prompt="x", image_path="photos/a.png")


def test_image_missing_rejected(tmp_path):
    with pytest.raises(ValidationError, match="не найден"):
        VideoRequest(prompt="x", image_path=str(tmp_path / "нет.png"))


def test_image_directory_rejected(tmp_path):
    with pytest.raises(ValidationError, match="не найден"):
        VideoRequest(prompt="x", image_path=str(tmp_path))


def test_image_wrong_extension_rejected(tmp_path):
    p = _img(tmp_path, "id_rsa", PNG)
    with pytest.raises(ValidationError, match="расширение"):
        VideoRequest(prompt="x", image_path=str(p))


def test_image_signature_mismatch_rejected(tmp_path):
    # произвольный файл, переименованный в .png, в приложение уходить не должен
    p = _img(tmp_path, "secret.png", b"ssh-rsa AAAA not an image")
    with pytest.raises(ValidationError, match="не похож"):
        VideoRequest(prompt="x", image_path=str(p))


def test_image_extension_must_match_content(tmp_path):
    p = _img(tmp_path, "a.jpg", PNG)
    with pytest.raises(ValidationError, match="не похож"):
        VideoRequest(prompt="x", image_path=str(p))


def test_image_too_large_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "MAX_IMAGE_BYTES", 10)
    p = _img(tmp_path, "big.png", PNG)
    with pytest.raises(ValidationError, match="велик"):
        VideoRequest(prompt="x", image_path=str(p))
