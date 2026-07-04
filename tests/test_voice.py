"""Транскрипция голосовых сообщений (OGG из Telegram) в текст — перенесено из
бот_напомилка_календарь: ffmpeg (OGG→WAV) + Google Speech Recognition
(бесплатно, без ключа). Позволяет ставить задачи боту голосом вместо текста."""
import subprocess
from pathlib import Path

import pytest
from content_factory.bot.voice import transcribe_voice_bytes


def test_transcribe_voice_bytes_runs_ffmpeg_then_recognizes(tmp_path):
    calls = {}

    def fake_ffmpeg(ogg_path, wav_path):
        calls["ogg_written"] = Path(ogg_path).read_bytes()
        Path(wav_path).write_bytes(b"WAVDATA")   # имитация результата конвертации

    def fake_recognize(wav_path):
        calls["wav_path"] = wav_path
        return "поставь стиральную машину бренд бэко"

    text = transcribe_voice_bytes(b"OGGBYTES", run_ffmpeg=fake_ffmpeg,
                                  recognize=fake_recognize)

    assert text == "поставь стиральную машину бренд бэко"
    assert calls["ogg_written"] == b"OGGBYTES"
    assert Path(calls["wav_path"]).name == "voice.wav"


def test_transcribe_voice_bytes_propagates_ffmpeg_error():
    def bad_ffmpeg(ogg_path, wav_path):
        raise subprocess.CalledProcessError(1, "ffmpeg")

    with pytest.raises(subprocess.CalledProcessError):
        transcribe_voice_bytes(b"OGGBYTES", run_ffmpeg=bad_ffmpeg,
                               recognize=lambda w: "x")


def test_transcribe_voice_bytes_cleans_up_temp_files(tmp_path):
    captured_paths = {}

    def fake_ffmpeg(ogg_path, wav_path):
        captured_paths["ogg"] = Path(ogg_path)
        captured_paths["wav"] = Path(wav_path)
        Path(wav_path).write_bytes(b"WAVDATA")

    transcribe_voice_bytes(b"OGGBYTES", run_ffmpeg=fake_ffmpeg,
                           recognize=lambda w: "текст")

    assert not captured_paths["ogg"].exists()   # временная папка удалена целиком
    assert not captured_paths["wav"].exists()
