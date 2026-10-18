"""Голос Джарвиса: запись с микрофона (VAD), локальный Whisper, русский TTS.

Все три компонента офлайн (приватность): синтез — голоса Windows (Irina/Pavel),
распознавание — faster-whisper small на CPU (модель держится в памяти между
фразами), запись — sounddevice. Ничего не уходит в сеть.
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
MODEL_DIR = Path(r"D:\hf-cache\faster_whisper")

_model = None  # ленивая загрузка и повторное использование


# --- распознавание ------------------------------------------------------------


def _load_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        started = time.perf_counter()
        _model = WhisperModel("small", device="cpu", compute_type="int8",
                              download_root=str(MODEL_DIR))
        print(f"[голос] модель Whisper загружена за {time.perf_counter() - started:.1f} с")
    return _model


def transcribe(audio: np.ndarray) -> dict:
    """numpy float32 16k mono → текст (ru). Замер показал beam=1 без VAD ≈ 4 с
    на 4.6-секундную фразу; внутренний VAD держим выключенным — режем тишину сами."""
    model = _load_model()
    started = time.perf_counter()
    segments, info = model.transcribe(audio, language="ru", beam_size=1,
                                      vad_filter=False,
                                      condition_on_previous_text=False)
    text = " ".join(segment.text.strip() for segment in segments).strip()
    return {"text": text, "ms": round((time.perf_counter() - started) * 1000),
            "language": info.language, "probability": round(info.language_probability, 2)}


# --- запись с VAD -------------------------------------------------------------


def record_until_silence(max_seconds: float = 12.0, initial_timeout_s: float = 6.0,
                         silence_s: float = 0.8, energy: float = 0.008,
                         device=None) -> np.ndarray:
    """Пишет с микрофона до паузы: речь начинается по порогу энергии, кончается
    после `silence_s` тишины. Возвращает float32-массив (пустой — если не заговорили).
    `device` — индекс/имя выбранного микрофона (None — системное по умолчанию)."""
    import sounddevice as sd

    block = 0.1  # секунды
    frames: list[np.ndarray] = []
    speaking = False
    silence_run = 0.0
    waited = 0.0
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        device=device) as stream:
        while True:
            chunk, _overflow = stream.read(int(block * SAMPLE_RATE))
            mono = chunk[:, 0]
            rms = float(np.sqrt(np.mean(mono ** 2)))
            if not speaking:
                waited += block
                if rms >= energy:
                    speaking = True
                    frames.append(mono.copy())
                elif waited >= initial_timeout_s:
                    return np.array([], dtype="float32")
                continue
            frames.append(mono.copy())
            silence_run = silence_run + block if rms < energy else 0.0
            if silence_run >= silence_s or len(frames) * block >= max_seconds:
                break
    return np.concatenate(frames) if frames else np.array([], dtype="float32")


def wav_to_audio(path: Path) -> np.ndarray:
    """WAV (16-бит) → numpy float32 16k mono — вход для transcribe (без av)."""
    import wave

    with wave.open(str(path), "rb") as handle:
        channels, rate = handle.getnchannels(), handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    data = np.frombuffer(frames, dtype=np.int16).astype("float32") / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data


# --- синтез и воспроизведение -------------------------------------------------


def synthesize(text: str, path: Path) -> Path:
    """Русский голос Windows → WAV-файл (офлайн)."""
    import asyncio

    from winsdk.windows.media.speechsynthesis import SpeechSynthesizer
    from winsdk.windows.storage.streams import DataReader

    async def run() -> bytes:
        tts = SpeechSynthesizer()
        for voice in SpeechSynthesizer.all_voices:
            if voice.language == "ru-RU":
                tts.voice = voice
                break
        stream = await tts.synthesize_text_to_stream_async(text)
        reader = DataReader(stream.get_input_stream_at(0))
        await reader.load_async(stream.size)
        return bytes(reader.read_buffer(stream.size))

    data = asyncio.run(run())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def speak(text: str, workdir: Path | None = None) -> None:
    """Синтез и воспроизведение ответа (блокирующе, простыми средствами winsound)."""
    import winsound

    target = (workdir or Path(__file__).resolve().parents[2] / "logs" / "assistant") \
        / "reply.wav"
    synthesize(text, target)
    winsound.PlaySound(str(target), winsound.SND_FILENAME)
