"""Голоса для прослушивания: Edge (нейро, онлайн) и Silero (нейро, офлайн).

Синтезирует одну и ту же фразу несколькими голосами и сохраняет рядом —
владелец слушает и выбирает; выбранное потом фиксируется в конфиге пикером.

Запуск: .venv\\Scripts\\python.exe -u spike/tts_samples.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "logs" / "assistant" / "samples"
PHRASE = "Джарвис на связи. Включаю свежие новости на весь экран."
EDGE_VOICES = ["ru-RU-SvetlanaNeural", "ru-RU-DmitryNeural", "ru-RU-DariyaNeural"]
SILERO_SPEAKERS = ["baya", "aidar", "eugene"]


async def edge_sample(voice: str) -> Path:
    import edge_tts

    target = OUT / f"edge_{voice.split('-')[-1]}.mp3"
    communicate = edge_tts.Communicate(PHRASE, voice)
    with target.open("wb") as handle:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                handle.write(chunk["data"])
    return target


def silero_sample(speaker: str) -> Path:
    import torch

    model, _ = torch.hub.load("snakers4/silero-models", model="silero_tts",
                              language="ru", speaker="v3_1_ru", trust_repo=True)
    audio = model.apply_tts(text=PHRASE, speaker=speaker, sample_rate=48000)
    target = OUT / f"silero_{speaker}.wav"
    import soundfile as sf  # нет в venv — запасной путь ниже

    sf.write(str(target), audio.numpy(), 48000)
    return target


def silero_sample_wave(speaker: str) -> Path:
    """Без soundfile: тензор → 16-битный WAV стандартной библиотекой."""
    import wave

    import torch

    model, _ = torch.hub.load("snakers4/silero-models", model="silero_tts",
                              language="ru", speaker="v3_1_ru", trust_repo=True)
    audio = model.apply_tts(text=PHRASE, speaker=speaker, sample_rate=48000)
    pcm = (audio.clamp(-1, 1).numpy() * 32767).astype("int16")
    target = OUT / f"silero_{speaker}.wav"
    with wave.open(str(target), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(48000)
        handle.writeframes(pcm.tobytes())
    return target


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    print("— Edge TTS (нейро, онлайн):")
    for voice in EDGE_VOICES:
        try:
            path = asyncio.run(edge_sample(voice))
            print(f"   {voice:<28} {path.stat().st_size:>7} байт  {path.name}")
        except Exception as exc:  # noqa: BLE001 — проба
            print(f"   {voice}: ошибка {type(exc).__name__}: {str(exc)[:120]}")
    print("— Silero (нейро, офлайн; первая загрузка модели — минуты):")
    for speaker in SILERO_SPEAKERS:
        try:
            path = silero_sample_wave(speaker)
            print(f"   {speaker:<28} {path.stat().st_size:>7} байт  {path.name}")
        except Exception as exc:  # noqa: BLE001 — проба
            print(f"   {speaker}: ошибка {type(exc).__name__}: {str(exc)[:160]}")
    print(f"\nПапка для прослушивания: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
