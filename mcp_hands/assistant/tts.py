"""Голоса ассистента: три провайдера синтеза с выбором и настройкой.

- `piper`   — нейросетевой, ОФЛАЙН (модели в models/piper; Ирина, Руслан);
- `edge`    — нейросетевой Microsoft, ОНЛАЙН (фраза уходит в сервис):
              ru-RU-SvetlanaNeural, ru-RU-DmitryNeural;
- `windows` — системные Windows-голоса (Irina, Pavel) — офлайн, «робот», запасной.

Выбор (провайдер, голос, скорость, тон) живёт в config.local.json → assistant.tts
и меняется пикером `tools/setup_voice.py`. Воспроизведение файлов — MCI (winmm):
играет и WAV, и MP3 без внешних зависимостей.
"""

from __future__ import annotations

import ctypes
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # корень проекта (mcp_hands/assistant/tts.py)
PIPER_DIR = ROOT / "models" / "piper"
CONFIG_PATH = ROOT / "config.local.json"
WORKDIR = ROOT / "logs" / "assistant"

PIPER_LABELS = {"ru_RU-irina-medium": "Ирина (локально)", "ru_RU-ruslan-medium": "Руслан (локально)"}
EDGE_CANDIDATES = ["ru-RU-SvetlanaNeural", "ru-RU-DmitryNeural"]


# --- конфиг -------------------------------------------------------------------


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return data.get("assistant", {}) or {}
        except json.JSONDecodeError:
            return {}
    return {}


def save_config(patch: dict) -> None:
    """Аккуратно вписывает секцию assistant в общий конфиг, не трогая llm."""
    data: dict = {}
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    assistant = data.get("assistant") or {}
    for key, value in patch.items():
        if isinstance(value, dict):
            assistant[key] = {**(assistant.get(key) or {}), **value}
        else:
            assistant[key] = value
    data["assistant"] = assistant
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def current_choice() -> dict:
    """Действующий выбор голоса с дефолтами (piper/Ирина, если модель на месте)."""
    tts = (load_config().get("tts") or {})
    default_provider = "piper" if piper_voices() else "windows"
    choice = {"provider": tts.get("provider") or default_provider,
              "voice": tts.get("voice") or (piper_voices()[0] if default_provider == "piper"
                                            else default_windows_voice()),
              "rate": tts.get("rate", "+0%"), "pitch": tts.get("pitch", "+0Hz"),
              "volume": tts.get("volume", "+0%")}
    return choice


# --- каталоги голосов ---------------------------------------------------------


def piper_voices() -> list[str]:
    if not PIPER_DIR.exists():
        return []
    return sorted(path.stem for path in PIPER_DIR.glob("*.onnx"))


def windows_voices() -> list[dict]:
    import asyncio

    from winsdk.windows.media.speechsynthesis import SpeechSynthesizer

    async def run() -> list[dict]:
        return [{"id": voice.display_name, "label": f"{voice.display_name} "
                 f"({voice.language})", "language": voice.language}
                for voice in SpeechSynthesizer.all_voices]

    try:
        return asyncio.run(run())
    except Exception:  # noqa: BLE001 — каталог недоступен
        return []


def default_windows_voice() -> str:
    voices = windows_voices()
    for voice in voices:
        if voice["language"] == "ru-RU":
            return voice["id"]
    return voices[0]["id"] if voices else "Microsoft Irina"


def edge_voices(language_prefix: str = "ru") -> list[dict]:
    """Каталог Edge-голосов (онлайн-запрос списка; офлайн — предустановленный список)."""
    import asyncio

    try:
        import edge_tts

        async def run() -> list[dict]:
            voices = await edge_tts.list_voices()
            return [{"id": voice["ShortName"],
                     "label": f"{voice['ShortName']} ({voice['Gender']})"}
                    for voice in voices if voice["Locale"].startswith(f"{language_prefix}-")]

        return asyncio.run(run())
    except Exception:  # noqa: BLE001 — сеть/сервис недоступны: отдаём известные голоса
        return [{"id": name, "label": name} for name in EDGE_CANDIDATES]


def list_voices(provider: str) -> list[dict]:
    if provider == "piper":
        return [{"id": name, "label": PIPER_LABELS.get(name, name)} for name in piper_voices()]
    if provider == "edge":
        return edge_voices()
    if provider == "windows":
        return [{**voice, "label": voice["label"]} for voice in windows_voices()]
    return []


# --- синтез -------------------------------------------------------------------


def _play_mci(path: Path) -> None:
    """Проигрывание WAV/MP3 через MCI (winmm) с ожиданием конца — без зависимостей."""
    winmm = ctypes.windll.winmm
    alias = f"jarvis_{int(time.time() * 1000) % 100000}"
    open_cmd = f'open "{path}" type mpegvideo alias {alias}'
    if winmm.mciSendStringW(open_cmd, None, 0, None) != 0:
        winmm.mciSendStringW(f'open "{path}" alias {alias}', None, 0, None)
    winmm.mciSendStringW(f"set {alias} time format milliseconds", None, 0, None)
    length = ctypes.create_unicode_buffer(64)
    winmm.mciSendStringW(f"status {alias} length", length, 64, None)
    try:
        duration_ms = int(length.value or "0")
    except ValueError:
        duration_ms = 0
    winmm.mciSendStringW(f"play {alias}", None, 0, None)
    deadline = time.time() + max(duration_ms / 1000 + 2, 2)
    status = ctypes.create_unicode_buffer(64)
    while time.time() < deadline:
        winmm.mciSendStringW(f"status {alias} mode", status, 64, None)
        if status.value.strip() in ("stopped", ""):
            break
        time.sleep(0.1)
    winmm.mciSendStringW(f"close {alias}", None, 0, None)


def _synth_piper(text: str, voice: str, out_path: Path) -> Path:
    import subprocess
    import sys

    model = PIPER_DIR / f"{voice}.onnx"
    if not model.exists():
        raise RuntimeError(f"модель Piper не найдена: {model}")
    run = subprocess.run([sys.executable, "-m", "piper", "--model", str(model),
                          "--output_file", str(out_path)],
                         input=text.encode("utf-8"), capture_output=True, timeout=120)
    if not out_path.exists() or out_path.stat().st_size < 4000:
        raise RuntimeError(f"Piper не синтезировал: {run.stderr.decode('utf-8', 'replace')[-200:]}")
    return out_path


def _synth_edge(text: str, voice: str, out_path: Path, rate: str, pitch: str,
                volume: str) -> Path:
    import asyncio

    import edge_tts

    async def run() -> None:
        communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch,
                                           volume=volume)
        with out_path.open("wb") as handle:
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    handle.write(chunk["data"])

    asyncio.run(run())
    if not out_path.exists() or out_path.stat().st_size < 1000:
        raise RuntimeError("Edge TTS не вернул аудио (голос/сеть?)")
    return out_path


def _synth_windows(text: str, voice: str, out_path: Path, rate_slow: int = 0) -> Path:
    import asyncio

    from winsdk.windows.media.speechsynthesis import SpeechSynthesizer
    from winsdk.windows.storage.streams import DataReader

    async def run() -> bytes:
        tts = SpeechSynthesizer()
        for item in SpeechSynthesizer.all_voices:
            if item.display_name == voice or item.language == "ru-RU" and voice in (
                    "Microsoft Irina", ""):
                tts.voice = item
                break
        stream = await tts.synthesize_text_to_stream_async(text)
        reader = DataReader(stream.get_input_stream_at(0))
        await reader.load_async(stream.size)
        return bytes(reader.read_buffer(stream.size))

    data = asyncio.run(run())
    out_path.write_bytes(data)
    return out_path


def synthesize(text: str, choice: dict | None = None,
               out_path: Path | None = None) -> Path:
    """Синтез по выбору (или текущему из конфига). Возвращает путь к аудиофайлу."""
    picked = dict(choice or current_choice())
    provider = picked.get("provider") or "piper"
    voice = picked.get("voice") or ""
    WORKDIR.mkdir(parents=True, exist_ok=True)
    target = out_path or (WORKDIR / f"reply.{'mp3' if provider == 'edge' else 'wav'}")
    if provider == "piper":
        return _synth_piper(text, voice, target)
    if provider == "edge":
        return _synth_edge(text, voice, target, picked.get("rate", "+0%"),
                           picked.get("pitch", "+0Hz"), picked.get("volume", "+0%"))
    return _synth_windows(text, voice, target)


def speak(text: str, choice: dict | None = None) -> Path:
    """Синтезирует и проигрывает ответ (блокирующе)."""
    path = synthesize(text, choice)
    _play_mci(path)
    return path


def speak_with(provider: str, voice: str, text: str, rate: str = "+0%",
               pitch: str = "+0Hz") -> Path:
    """Пример для пикера: конкретный голос, без сохранения выбора."""
    return speak(text, {"provider": provider, "voice": voice, "rate": rate, "pitch": pitch})
