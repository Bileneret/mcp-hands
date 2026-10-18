"""Спайк тракта Джарвиса: TTS→Whisper (круговой), запись микрофона, hotkey.

Доказывает три опоры до сборки ассистента:
1. русская речь синтезируется (Irina) и РАСПОЗНАЁТСЯ локальным Whisper;
2. микрофон пишется (уровень сигнала за 2 с);
3. глобальный Ctrl+Alt+J можно зарегистрировать и снять (будущий триггер).

Запуск: .venv\\Scripts\\python.exe -u spike\\jarvis_probe.py
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MODEL_DIR = Path(r"D:\hf-cache\faster_whisper")
PHRASE = "Джарвис, открой YouTube и найди свежие новости"


def synth(text: str) -> Path:
    """Русский голос Windows → WAV (офлайн)."""
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
    out = ROOT / "logs" / "assistant" / "probe_synth.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"1. TTS: {len(data)} байт → {out.name}")
    return out


def read_wav_16k(path: Path):
    """WAV → numpy float32 16 кГц mono (без av: совместимость важнее форматов).

    faster-whisper 1.2.1 зовёт av.open(metadata_errors=…), которого в av 19 уже
    нет — поэтому декодируем сами; наш тракт всегда даёт WAV 16k mono (TTS
    Windows и запись sounddevice настроены именно так).
    """
    import wave

    import numpy as np

    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        rate = handle.getframerate()
        width = handle.getsampwidth()
        frames = handle.readframes(handle.getnframes())
    if width != 2:
        raise RuntimeError(f"ожидался 16-битный WAV, получено {width * 8} бит")
    data = np.frombuffer(frames, dtype=np.int16).astype("float32") / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    if rate != 16000:
        # линейная передискретизация достаточна для речевого распознавания
        target = int(len(data) * 16000 / rate)
        data = np.interp(np.linspace(0, len(data) - 1, target),
                         np.arange(len(data)), data).astype("float32")
    return data


def whisper_recognize(path: Path) -> str:
    """Локальный Whisper (small, CPU int8): распознать запись как numpy-массив."""
    from faster_whisper import WhisperModel

    audio = read_wav_16k(path)
    started = time.perf_counter()
    model = WhisperModel("small", device="cpu", compute_type="int8",
                         download_root=str(MODEL_DIR))
    load_s = time.perf_counter() - started
    started = time.perf_counter()
    segments, info = model.transcribe(audio, language="ru", beam_size=3,
                                      vad_filter=True)
    text = " ".join(segment.text.strip() for segment in segments).strip()
    print(f"2. Whisper: загрузка {load_s:.1f} с, распознавание "
          f"{time.perf_counter() - started:.1f} с | язык {info.language} "
          f"(p={info.language_probability:.2f})")
    return text


def microphone_level(seconds: float = 2.0) -> float:
    """Короткая запись с микрофона: RMS-уровень (микрофон жив)."""
    import numpy as np
    import sounddevice as sd

    recording = sd.rec(int(seconds * 16000), samplerate=16000, channels=1,
                       dtype="float32")
    sd.wait()
    level = float(np.sqrt(np.mean(recording ** 2)))
    print(f"3. Микрофон: {seconds:.0f} с записано, RMS={level:.4f} "
          f"({'есть сигнал' if level > 0.0005 else 'тишина/нет доступа'})")
    return level


def hotkey_probe() -> bool:
    """Регистрация глобального Ctrl+Alt+J (триггер Джарвиса) и снятие."""
    user32 = ctypes.windll.user32
    MOD_CONTROL, MOD_ALT = 0x0002, 0x0001
    hotkey_id = 0xA70F
    ok = bool(user32.RegisterHotKey(None, hotkey_id, MOD_CONTROL | MOD_ALT, ord("J")))
    if ok:
        user32.UnregisterHotKey(None, hotkey_id)
    print(f"4. Hotkey Ctrl+Alt+J: {'можно регистрировать' if ok else 'ЗАНЯТ чем-то'}")
    return ok


def main() -> int:
    wav = synth(PHRASE)
    text = whisper_recognize(wav)
    print(f"   распознано: {text!r}")
    roundtrip = "youtube" in text.lower().replace("ютуб", "youtube") or "новост" in text.lower()
    print(f"   круговой тест TTS→STT: {'СОВПАЛО' if roundtrip else 'РАСХОЖДЕНИЕ'}")
    microphone_level()
    hotkey_probe()
    return 0 if roundtrip else 1


if __name__ == "__main__":
    raise SystemExit(main())
