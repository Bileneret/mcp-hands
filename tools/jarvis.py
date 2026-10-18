"""Джарвис: голосовой ассистент поверх MCP-Hands.

Один запуск — и слушает. Ctrl+Alt+J в любой момент: «Слушаю…» → говоришь →
локальный Whisper распознаёт → локальные интенты или старший мозг (GLM) →
действия живыми инструментами → ответ русским голосом Windows.

Ничего не уходит в облако, кроме сложных фраз мозгу (и только текст).
Микрофон, синтез и распознавание — офлайн. Окна пользователя не трогаются
без явных инструментов; стоп-листы проекта действуют.

Запуск: .venv\\Scripts\\python.exe -u tools\\jarvis.py     (или jarvis.bat)
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import queue
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcp_hands.assistant.agent import Assistant  # noqa: E402
from mcp_hands.assistant.overlay import Indicator  # noqa: E402
from mcp_hands.assistant.voice import record_until_silence, speak, transcribe  # noqa: E402

HOTKEY_ID = 0xA70F
MOD_ALT, MOD_CONTROL = 0x0001, 0x0002
WM_HOTKEY = 0x0312

_events: "queue.Queue[str]" = queue.Queue()


def hotkey_listener() -> None:
    """Свой поток: RegisterHotKey привязан к потоку, ждём WM_HOTKEY и передаём в очередь."""
    user32 = ctypes.windll.user32
    if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_ALT, ord("J")):
        print("!! Ctrl+Alt+J занят другой программой — закройте её и перезапустите Джарвиса")
        return
    message = ctypes.wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(message), None, 0, 0) != 0:
        if message.message == WM_HOTKEY and message.wParam == HOTKEY_ID:
            _events.put("hotkey")
    user32.UnregisterHotKey(None, HOTKEY_ID)


def worker(assistant: Assistant, indicator: Indicator) -> None:
    """Обработка нажатий: запись → распознавание → маршрут → голос."""
    print("Джарвис готов. Ctrl+Alt+J — говорите. Ctrl+C — выход.")
    while True:
        event = _events.get()
        if event == "stop":
            return
        indicator.set("listening")
        audio = record_until_silence()
        if audio.size == 0 or len(audio) / 16000 < 0.3:
            indicator.set("talking", "Не услышал")
            speak("Не услышал, повторите.")
            indicator.set("idle")
            continue
        indicator.set("thinking")
        heard = transcribe(audio)
        text = heard["text"].strip()
        print(f"[{time.strftime('%H:%M:%S')}] вы: {text!r} ({heard['ms']} мс)")
        if not text:
            indicator.set("talking", "Пустая фраза")
            speak("Не разобрал слова.")
            indicator.set("idle")
            continue
        indicator.set("working", text[:40])
        result = assistant.handle(text)
        print(f"    джарвис: {result['say'][:120]} ({result['kind']})")
        indicator.set("talking", result["say"][:42])
        speak(result["say"])
        time.sleep(0.6)
        indicator.set("idle")


def main() -> int:
    assistant = Assistant(force_work="--work" in sys.argv[1:])

    # Режимы приёмки без микрофона (отладка и автотесты):
    #   --say "текст"     — пропустить распознавание, сразу маршрут по тексту
    #   --wav <file.wav>  — прогнать файл через распознавание и маршрут
    argv = sys.argv[1:]
    if "--say" in argv:
        text = argv[argv.index("--say") + 1]
        result = assistant.handle(text)
        print(f"вы: {text!r}\nджарвис: {result['say']} ({result['kind']})")
        speak(result["say"])
        return 0 if result["kind"] else 1
    if "--wav" in argv:
        from mcp_hands.assistant.voice import wav_to_audio

        wav = Path(argv[argv.index("--wav") + 1])
        heard = transcribe(wav_to_audio(wav))
        print(f"распознано ({heard['ms']} мс): {heard['text']!r}")
        result = assistant.handle(heard["text"])
        print(f"джарвис: {result['say']} ({result['kind']})")
        speak(result["say"])
        return 0 if result["kind"] else 1

    indicator = Indicator()
    indicator.start()
    threading.Thread(target=hotkey_listener, daemon=True, name="jarvis-hotkey").start()
    time.sleep(1.5)  # плашка и hotkey успевают встать
    try:
        worker(assistant, indicator)
    except KeyboardInterrupt:
        print("\nДжарвис выключен.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
