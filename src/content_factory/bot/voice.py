"""Транскрипция голосовых сообщений Telegram (OGG) в текст — перенесено из
бот_напомилка_календарь: ffmpeg (OGG→WAV, 16 кГц моно) + Google Speech
Recognition (бесплатно, без ключа/квот). Транскрибированный текст идёт в ту же
обработку, что обычный текст (визард /task, /make, /find и т.д.) — владелец
может ставить задачи голосом вместо набора.

⚠️ Модели/артикулы (буквы+цифры вперемешку, напр. «F1096ND3») речь распознаёт
ненадёжно — голос удобен для категорий/команд, для точных строк списка в /task
надёжнее печатать."""
from __future__ import annotations
import subprocess
import tempfile
from pathlib import Path

import speech_recognition as sr


def _run_ffmpeg(ogg_path, wav_path) -> None:
    subprocess.run(["ffmpeg", "-y", "-i", str(ogg_path), "-ar", "16000", "-ac", "1",
                    str(wav_path)], check=True, capture_output=True)


def _recognize_google(wav_path) -> str:
    recognizer = sr.Recognizer()
    with sr.AudioFile(str(wav_path)) as source:
        audio = recognizer.record(source)
    return recognizer.recognize_google(audio, language="ru-RU")


def transcribe_voice_bytes(ogg_bytes: bytes, run_ffmpeg=None, recognize=None) -> str:
    """Голосовое (OGG-байты из Telegram) -> распознанный текст.
    run_ffmpeg(ogg_path, wav_path)/recognize(wav_path) — инъекция для тестов,
    по умолчанию реальный ffmpeg-конвертер + Google Speech Recognition."""
    run_ffmpeg = run_ffmpeg or _run_ffmpeg
    recognize = recognize or _recognize_google
    with tempfile.TemporaryDirectory() as td:
        ogg_path = Path(td) / "voice.ogg"
        wav_path = Path(td) / "voice.wav"
        ogg_path.write_bytes(ogg_bytes)
        run_ffmpeg(ogg_path, wav_path)
        return recognize(wav_path)
