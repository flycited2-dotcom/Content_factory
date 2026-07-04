"""Транскрипция голосовых сообщений Telegram (OGG) в текст — перенесено и
доработано из бот_напомилка_календарь: ffmpeg (OGG→WAV) + Google Speech
Recognition (бесплатно, без ключа/квот). Транскрибированный текст идёт в ту же
обработку, что обычный текст (визард /task, /make, /find и т.д.) — владелец
может ставить задачи голосом вместо набора.

Доработки относительно первоисточника (для стабильности на реальных голосовых
из Telegram — сжатый OPUS, разная громкость, фоновый шум, диктовка списков
длиннее одной фразы):
  1. Предобработка звука перед распознаванием: полосовой фильтр (речевой
     диапазон), громкостная нормализация (тихая запись — частая причина
     промахов), обрезка тишины по краям.
  2. Длинные голосовые бьются на куски ~50с — свободный Google-эндпоинт
     расcчитан на короткие команды и ненадёжен на длинных записях; кусок без
     распознанной речи тихо пропускается, не обрывая всё сообщение.
  3. Ретрай на сетевую ошибку распознавания (RequestError) — НЕ на «не понял»
     (UnknownValueError): звук не меняется, повтор бессмысленен.

⚠️ Модели/артикулы (буквы+цифры вперемешку, напр. «F1096ND3») речь распознаёт
ненадёжно — голос удобен для категорий/команд, для точных строк списка в /task
надёжнее печатать."""
from __future__ import annotations
import subprocess
import tempfile
import time
from pathlib import Path

import speech_recognition as sr

_CHUNK_SECONDS = 50    # запас ниже практического лимита свободного Google-эндпоинта (~60с)
_RETRIES = 2           # повторов на сетевую ошибку одного куска (минимум 1 — см. assert)

assert _RETRIES >= 1, "_RETRIES < 1 оставит last_err=None и сломает raise ниже"


def _run_ffmpeg(ogg_path, wav_path) -> None:
    """OGG → WAV 16кГц моно + предобработка речи (см. докстринг модуля)."""
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(ogg_path),
             "-af", "highpass=f=100,lowpass=f=8000,loudnorm=I=-16:TP=-1.5:LRA=11,"
                    "silenceremove=start_periods=1:start_threshold=-35dB:"
                    "stop_periods=1:stop_threshold=-35dB:stop_duration=0.5",
             "-ar", "16000", "-ac", "1", str(wav_path)],
            check=True, capture_output=True, timeout=30)
    except subprocess.CalledProcessError as e:
        # По умолчанию str(e) — только «returned non-zero exit status N», без
        # причины; stderr ffmpeg — единственное, что реально объясняет сбой.
        stderr = (e.stderr or b"").decode(errors="ignore")[-500:]
        raise RuntimeError(f"ffmpeg: {stderr or e}") from e


def _recognize_chunk(recognizer: sr.Recognizer, audio) -> str:
    """Одна попытка распознать кусок звука; ретрай — только на сетевую ошибку."""
    last_err: Exception | None = None
    for _ in range(_RETRIES):
        try:
            return recognizer.recognize_google(audio, language="ru-RU")
        except sr.RequestError as e:
            last_err = e
            time.sleep(0.5)
    raise last_err


def _recognize_google(wav_path) -> str:
    """Распознаёт весь файл кусками по _CHUNK_SECONDS (см. докстринг модуля,
    пункт 2). Кусок без распознанной речи пропускается; если ни один кусок не
    распознался — UnknownValueError (как и родной recognize_google на пустом
    звуке), чтобы вызывающий код увидел понятную ошибку, а не тихий пустой текст."""
    recognizer = sr.Recognizer()
    results = []
    with sr.AudioFile(str(wav_path)) as source:
        offset = 0.0
        while offset < source.DURATION:
            audio = recognizer.record(source, duration=_CHUNK_SECONDS)
            try:
                text = _recognize_chunk(recognizer, audio)
                if text:
                    results.append(text)
            except sr.UnknownValueError:
                pass
            offset += _CHUNK_SECONDS
    if not results:
        raise sr.UnknownValueError("речь не распознана ни в одном фрагменте")
    return " ".join(results).strip()


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
