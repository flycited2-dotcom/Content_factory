"""Транскрипция голосовых сообщений (OGG из Telegram) в текст — перенесено из
бот_напомилка_календарь: ffmpeg (OGG→WAV) + Google Speech Recognition
(бесплатно, без ключа). Позволяет ставить задачи боту голосом вместо текста."""
import subprocess
import wave
from pathlib import Path

import pytest
import speech_recognition as sr
from content_factory.bot import voice as voice_mod
from content_factory.bot.voice import transcribe_voice_bytes


def _make_wav(path, seconds: float, rate: int = 16000) -> None:
    """Синтетический тихий WAV нужной длительности — для проверки реальной
    логики нарезки на куски (_recognize_google) без обращения к сети/ffmpeg."""
    n_frames = int(seconds * rate)
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * n_frames)


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


# ── _recognize_google: реальная нарезка длинных записей на куски ─────────────
# (сеть мокается, WAV — настоящий синтетический файл, не мок)
def test_recognize_google_chunks_long_audio(tmp_path, monkeypatch):
    wav_path = tmp_path / "long.wav"
    _make_wav(wav_path, seconds=65)                # 65с > 50с чанк → 2 итерации

    calls = []

    def fake_recognize_google(self, audio, language=None):
        calls.append(language)
        return f"кусок{len(calls)}"

    monkeypatch.setattr(sr.Recognizer, "recognize_google", fake_recognize_google)
    text = voice_mod._recognize_google(wav_path)

    assert len(calls) == 2 and all(lang == "ru-RU" for lang in calls)
    assert text == "кусок1 кусок2"


def test_recognize_google_short_audio_single_call(tmp_path, monkeypatch):
    wav_path = tmp_path / "short.wav"
    _make_wav(wav_path, seconds=3)

    calls = []
    monkeypatch.setattr(sr.Recognizer, "recognize_google",
                       lambda self, audio, language=None: calls.append(1) or "привет")
    text = voice_mod._recognize_google(wav_path)

    assert len(calls) == 1 and text == "привет"


def test_recognize_google_skips_unrecognized_chunk(tmp_path, monkeypatch):
    wav_path = tmp_path / "long.wav"
    _make_wav(wav_path, seconds=65)

    def fake_recognize(self, audio, language=None):
        fake_recognize.n = getattr(fake_recognize, "n", 0) + 1
        if fake_recognize.n == 1:
            raise sr.UnknownValueError()            # первый кусок — тишина/шум
        return "второй кусок понятен"

    monkeypatch.setattr(sr.Recognizer, "recognize_google", fake_recognize)
    text = voice_mod._recognize_google(wav_path)

    assert text == "второй кусок понятен"            # непонятный кусок не оборвал всё


def test_recognize_google_raises_when_all_chunks_unrecognized(tmp_path, monkeypatch):
    wav_path = tmp_path / "short.wav"
    _make_wav(wav_path, seconds=3)

    def always_fail(self, audio, language=None):
        raise sr.UnknownValueError()
    monkeypatch.setattr(sr.Recognizer, "recognize_google", always_fail)

    with pytest.raises(sr.UnknownValueError):
        voice_mod._recognize_google(wav_path)


# ── _run_ffmpeg: реальный сбой ffmpeg должен давать понятную причину ─────────
def test_run_ffmpeg_wraps_calledprocesserror_with_stderr(monkeypatch, tmp_path):
    def fake_run(*a, **kw):
        raise subprocess.CalledProcessError(
            1, "ffmpeg", stderr=b"Invalid data found when processing input\n")
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Invalid data found"):
        voice_mod._run_ffmpeg(tmp_path / "in.ogg", tmp_path / "out.wav")


def test_recognize_google_retries_on_network_error_then_succeeds(tmp_path, monkeypatch):
    wav_path = tmp_path / "short.wav"
    _make_wav(wav_path, seconds=3)

    attempts = []

    def flaky(self, audio, language=None):
        attempts.append(1)
        if len(attempts) == 1:
            raise sr.RequestError("сеть моргнула")
        return "распозналось со второй попытки"
    monkeypatch.setattr(sr.Recognizer, "recognize_google", flaky)
    monkeypatch.setattr(voice_mod.time, "sleep", lambda s: None)  # не ждать в тесте

    text = voice_mod._recognize_google(wav_path)
    assert len(attempts) == 2 and text == "распозналось со второй попытки"
