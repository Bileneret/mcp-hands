"""Настройка голоса Джарвиса: микрофон + голос синтеза — один раз, с прослушиванием.

Запуск: .venv\\Scripts\\python.exe -u tools\\setup_voice.py

Разделы:
  1) Микрофон: список устройств ввода, тест уровня («скажи что-нибудь»),
     выбор по номеру; сохраняется в config.local.json (assistant.mic.device).
  2) Голос: провайдер (piper — офлайн нейро / edge — онлайн нейро /
     windows — системный), выбор голоса, прослушивание одной фразы,
     настройка скорости и тона для edge; выбор сохраняется (assistant.tts).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcp_hands.assistant import mic, tts  # noqa: E402

PHRASE = "Джарвис на связи. Включаю свежие новости и разворачиваю на весь экран."


def setup_microphone() -> None:
    print("\n=== Микрофон ===")
    devices = mic.list_input_devices()
    if not devices:
        print("Устройства ввода не найдены (нет микрофона?).")
        return
    for device in devices:
        marker = " ★ по умолчанию" if device["is_default"] else ""
        chosen = " (текущий выбор)" if device["name"] == mic.current_device_name() else ""
        print(f"  [{device['index']:>2}] {device['name']}{marker}{chosen}")
    current = mic.current_device_name() or "системное по умолчанию"
    print(f"Сейчас: {current}")
    answer = input("Номер устройства (Enter — не менять, 't' — тест текущего): ").strip()
    if answer.lower() == "t":
        print("Говори в микрофон 3 секунды…")
        result = mic.test_input(3.0)
        print(f"  уровень: {result['level']} — "
              f"{'сигнал есть' if result['has_signal'] else 'тишина (проверь выбор!)'}")
        answer = input("Номер устройства (Enter — не менять): ").strip()
    if answer.isdigit():
        picked = next((d for d in devices if d["index"] == int(answer)), None)
        if picked:
            mic.set_device(picked["name"])
            print(f"Микрофон сохранён: {picked['name']}")
            print("Проверяю запись… Говори 3 секунды!")
            result = mic.test_input(3.0, device=picked["index"])
            print(f"  уровень: {result['level']} — "
                  f"{'ок' if result['has_signal'] else 'тишина: возможно, выбран не тот вход'}")
        else:
            print("Такого номера нет — оставляю как было.")


def setup_voice() -> None:
    print("\n=== Голос ===")
    choice = tts.current_choice()
    print(f"Сейчас: {choice['provider']} / {choice['voice']} "
          f"(скорость {choice['rate']}, тон {choice['pitch']})")
    providers = [("piper", "локальный нейро (офлайн)"),
                 ("edge", "нейро Microsoft (онлайн)"),
                 ("windows", "системный Windows (робот, запасной)")]
    for index, (name, note) in enumerate(providers, 1):
        print(f"  {index}) {name:<8} — {note}")
    answer = input("Провайдер (Enter — не менять): ").strip()
    provider = choice["provider"]
    if answer.isdigit() and 1 <= int(answer) <= len(providers):
        provider = providers[int(answer) - 1][0]
    voices = tts.list_voices(provider)
    if not voices:
        print(f"У провайдера {provider} нет доступных голосов.")
        return
    print(f"Голоса {provider}:")
    for index, voice in enumerate(voices, 1):
        print(f"  [{index}] {voice['label']}  ({voice['id']})")
    rate, pitch = choice["rate"], choice["pitch"]
    if provider == "edge":
        rate = input(f"Скорость [{choice['rate']}] (например +15% или -10%): ").strip() \
            or choice["rate"]
        pitch = input(f"Тон [{choice['pitch']}] (например +5Hz): ").strip() or choice["pitch"]
    voice_id = input("Номер голоса (Enter — оставить текущий, 's' — прослушать все, "
                     "'p' — прослушать выбранный): ").strip()
    if voice_id.lower() == "s":
        for index, voice in enumerate(voices, 1):
            print(f"  слушай [{index}] {voice['label']}…")
            try:
                tts.speak_with(provider, voice["id"], PHRASE, rate, pitch)
            except Exception as exc:  # noqa: BLE001 — проба
                print(f"    ошибка: {exc}")
        voice_id = input("Номер понравившегося голоса: ").strip()
    picked_voice = voices[0]
    if voice_id.isdigit() and 1 <= int(voice_id) <= len(voices):
        picked_voice = voices[int(voice_id) - 1]
    elif voice_id.lower() == "p":
        picked_voice = next((v for v in voices if v["id"] == choice["voice"]), voices[0])
    print("Прослушай выбор:")
    try:
        tts.speak_with(provider, picked_voice["id"], PHRASE, rate, pitch)
    except Exception as exc:  # noqa: BLE001 — проба
        print(f"  ошибка синтеза: {exc}")
        return
    confirm = input("Сохранить этот голос? (Enter — да, 'n' — нет): ").strip().lower()
    if confirm != "n":
        tts.save_config({"tts": {"provider": provider, "voice": picked_voice["id"],
                                 "rate": rate, "pitch": pitch}})
        print(f"Голос сохранён: {provider} / {picked_voice['id']}")


def main() -> int:
    print("Настройка Джарвиса: микрофон и голос. Конфиг: config.local.json")
    setup_microphone()
    setup_voice()
    print("\nГотово. Запускай Джарвиса: jarvis.bat (Ctrl+Alt+J — говорить).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
