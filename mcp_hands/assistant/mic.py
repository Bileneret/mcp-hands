"""Микрофон ассистента: список устройств, выбор, тест уровня, вежливый откат.

Выбор устройства ввода живёт в config.local.json → assistant.mic.device (имя).
Если выбранное устройство исчезло (наушники отключили) — запись идёт на
устройство по умолчанию, и причина печатается: молчаливой записи тишины нет.
"""

from __future__ import annotations

import numpy as np

from .tts import load_config, save_config  # конфиг общий: одна секция assistant

SAMPLE_RATE = 16000


def list_input_devices() -> list[dict]:
    """Устройства ввода с признаком устройства по умолчанию."""
    import sounddevice as sd

    default_index = -1
    try:
        default_index = sd.default.device[0]
    except Exception:  # noqa: BLE001
        pass
    devices = []
    for index, device in enumerate(sd.query_devices()):
        if device.get("max_input_channels", 0) <= 0:
            continue
        devices.append({"index": index, "name": device["name"],
                        "channels": device["max_input_channels"],
                        "default_samplerate": int(device["default_samplerate"]),
                        "is_default": index == default_index})
    return devices


def current_device_name() -> str:
    return (load_config().get("mic") or {}).get("device", "")


def set_device(name: str) -> None:
    """Сохраняет выбор устройства (пустая строка = системное по умолчанию)."""
    save_config({"mic": {"device": name}})


def resolve_device() -> tuple[str | int | None, str]:
    """(устройство для sounddevice, примечание). По имени из конфига; иначе default."""
    chosen = current_device_name()
    if not chosen:
        return None, "системное устройство по умолчанию"
    lowered = chosen.lower()
    for device in list_input_devices():
        if device["name"].lower() == lowered:
            return device["index"], f"выбранный микрофон: {device['name']}"
    for device in list_input_devices():
        if lowered in device["name"].lower() or device["name"].lower() in lowered:
            return device["index"], f"выбранный микрофон (частичное совпадение): {device['name']}"
    return None, (f"микрофон {chosen!r} не найден — пишу с системного по умолчанию "
                  "(выбери заново: tools/setup_voice.py)")


def test_input(seconds: float = 3.0, device: str | int | None = None) -> dict:
    """Короткая запись: уровень сигнала (для пикера и диагностики)."""
    import sounddevice as sd

    target = device if device is not None else resolve_device()[0]
    recording = sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE,
                       channels=1, dtype="float32", device=target)
    sd.wait()
    level = float(np.sqrt(np.mean(recording ** 2)))
    return {"level": round(level, 4), "has_signal": level > 0.0008,
            "seconds": seconds, "device": target}
