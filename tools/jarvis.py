"""Джарвис: голосовой ассистент поверх MCP-Hands.

Один запуск — и разговор идёт подряд. Ctrl+Alt+J включает микрофон («Слушаю…»),
дальше фразы обрабатываются одна за другой: локальный Whisper распознаёт →
локальные интенты или старший мозг (GLM) → живой браузер/инструменты → ответ
русским голосом. Если пауза затянулась, ассистент засыпает (микрофон свободен),
следующее нажатие Ctrl+Alt+J будит его; «стоп»/«хватит» — тоже сон.

Ничего не уходит в облако, кроме сложных фраз мозгу (и только текст).
Микрофон, синтез и распознавание — офлайн. Окна пользователя не трогаются
без явных инструментов; стоп-листы проекта действуют.

Запуск: .venv\\Scripts\\python.exe -u tools\\jarvis.py     (или jarvis.bat)
Флаги: --once — одна фраза на нажатие (старое поведение);
       --sleep-after 90 — пауза до засыпания, секунды;
       --say "текст" / --wav file.wav — приёмка без микрофона; --work — свой браузер;
       --quiet — не произносить ответ вслух (проверки ночью).
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

from mcp_hands.assistant import mic, tts  # noqa: E402
from mcp_hands.assistant.agent import Assistant  # noqa: E402
from mcp_hands.assistant.overlay import Indicator  # noqa: E402
from mcp_hands.assistant.session import (  # noqa: E402
    sleep_after_seconds, is_stop_phrase, DEFAULT_SLEEP_AFTER_S)
from mcp_hands.assistant.voice import (  # noqa: E402
    SAMPLE_RATE, record_until_silence, transcribe)


QUIET = False  # --quiet: приёмка/автотесты без звука (владельца не будим)


def say(text: str) -> None:
    """Ответ голосом по выбору из конфига (piper/edge/windows); при сбое — запасной."""
    if QUIET:
        print(f"(без звука) {text}")
        return
    try:
        tts.speak(text)
    except Exception as exc:  # noqa: BLE001 — голос не должен ломать ассистента
        print(f"!! голос не сработал ({exc}); говорю системным")
        try:
            tts.speak(text, {"provider": "windows", "voice": "Microsoft Irina"})
        except Exception:  # noqa: BLE001
            print("(ответ не озвучен)")

HOTKEY_ID = 0xA70F
MOD_ALT, MOD_CONTROL = 0x0001, 0x0002
WM_HOTKEY = 0x0312

LISTEN_TIMEOUT_S = 8.0   # ждём начало фразы, прежде чем засчитать круг тишины
SPEECH_MARGIN_S = 0.3    # короче — считаем, что владелец не говорил

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


def beep(kind: str) -> None:
    """Сигнал состояния вместо слов: во время засыпания микрофон ещё слушает,
    и сказанная вслух фраза попала бы в запись."""
    import winsound

    pattern = {"wake": ((880, 90), (1180, 110)), "sleep": ((600, 120), (380, 190))}
    try:
        for frequency, duration in pattern.get(kind, ((700, 120),)):
            winsound.Beep(frequency, duration)
    except Exception:  # noqa: BLE001 — сигнал не обязателен
        pass


def pending_hotkey() -> bool:
    """Нажатие в очереди. Во время разговора это «замолчи» (выключаем микрофон)."""
    pressed = False
    try:
        while True:
            event = _events.get_nowait()
            if event == "stop":
                return True
            pressed = True
    except queue.Empty:
        pass
    return pressed


def resolve_mic() -> int | None:
    device, note = mic.resolve_device()
    if note and "по умолчанию" not in note and "выбранный" not in note:
        print(f"!! {note}")
    return device


def handle_phrase(assistant: Assistant, indicator: Indicator, audio) -> str:
    """Одна фраза: распознать → исполнить → озвучить. Возврат: "" | "empty" | "stop"."""
    if audio.size == 0 or len(audio) / SAMPLE_RATE < SPEECH_MARGIN_S:
        return "empty"
    indicator.set("thinking")
    heard = transcribe(audio)
    text = heard["text"].strip()
    print(f"[{time.strftime('%H:%M:%S')}] вы: {text!r} ({heard['ms']} мс)")
    if not text:
        return "empty"
    if is_stop_phrase(text):
        say("Хорошо, сплю.")
        return "stop"
    indicator.set("working", text[:40])
    result = assistant.handle(text)
    print(f"    джарвис: {result['say'][:120]} ({result['kind']})")
    indicator.set("talking", result["say"][:42])
    say(result["say"])
    time.sleep(0.3)  # хвост собственного голоса не должен попасть в следующую запись
    return ""


def conversation(assistant: Assistant, indicator: Indicator, device,
                 sleep_after_s: float) -> str:
    """Разговор до тишины или стоп-фразы. Возврат: "sleep" (пауза) | "stop" (остановка)."""
    last_voice = time.time()
    while True:
        if pending_hotkey():
            return "stop"
        if time.time() - last_voice >= sleep_after_s:
            return "sleep"
        audio = record_until_silence(device=device, initial_timeout_s=LISTEN_TIMEOUT_S)
        if audio.size == 0:
            continue  # тишина в пределах паузы: просто ждём дальше, не переспрашиваем
        outcome = handle_phrase(assistant, indicator, audio)
        if outcome == "stop":
            return "stop"
        last_voice = time.time()  # и вопрос, и ответ отодвигают засыпание
        indicator.set("listening")


def one_shot(assistant: Assistant, indicator: Indicator, device) -> None:
    """Старый режим: одна фраза на нажатие (для тех, кому так удобнее)."""
    audio = record_until_silence(device=device)
    if audio.size == 0 or len(audio) / SAMPLE_RATE < SPEECH_MARGIN_S:
        indicator.set("talking", "Не услышал")
        say("Не услышал, повторите.")
        return
    outcome = handle_phrase(assistant, indicator, audio)
    if outcome == "empty":
        indicator.set("talking", "Пустая фраза")
        say("Не разобрал слова.")


def worker(assistant: Assistant, indicator: Indicator, sleep_after_s: float,
           continuous: bool) -> None:
    """Ждёт пробуждения, ведёт разговор, засыпает по тишине."""
    while True:
        event = _events.get()
        if event == "stop":
            return
        device = resolve_mic()
        beep("wake")
        indicator.set("listening")
        if continuous:
            print(f"Слушаю… (пауза дольше {sleep_after_s:.0f} с — засыпаю; "
                  "«стоп» или Ctrl+Alt+J — тоже)")
            reason = conversation(assistant, indicator, device, sleep_after_s)
            indicator.set("sleeping")
            if reason == "sleep":
                beep("sleep")
            print("Заснул. Ctrl+Alt+J — разбудить." if reason == "sleep"
                  else "Микрофон выключен. Ctrl+Alt+J — включить.")
            continue
        one_shot(assistant, indicator, device)
        time.sleep(0.4)
        indicator.set("idle")


def main() -> int:
    global QUIET

    assistant = Assistant(force_work="--work" in sys.argv[1:])

    # Режимы приёмки без микрофона (отладка и автотесты):
    #   --say "текст"     — пропустить распознавание, сразу маршрут по тексту
    #   --wav <file.wav>  — прогнать файл через распознавание и маршрут
    #   --quiet           — не произносить ответ вслух (проверки ночью)
    argv = sys.argv[1:]
    QUIET = "--quiet" in argv
    if "--say" in argv:
        text = argv[argv.index("--say") + 1]
        result = assistant.handle(text)
        print(f"вы: {text!r}\nджарвис: {result['say']} ({result['kind']})")
        say(result["say"])
        return 0 if result["kind"] else 1
    if "--wav" in argv:
        from mcp_hands.assistant.voice import wav_to_audio

        wav = Path(argv[argv.index("--wav") + 1])
        heard = transcribe(wav_to_audio(wav))
        print(f"распознано ({heard['ms']} мс): {heard['text']!r}")
        result = assistant.handle(heard["text"])
        print(f"джарвис: {result['say']} ({result['kind']})")
        say(result["say"])
        return 0 if result["kind"] else 1

    continuous = "--once" not in argv
    sleep_after_s = DEFAULT_SLEEP_AFTER_S
    if "--sleep-after" in argv:
        try:
            sleep_after_s = float(argv[argv.index("--sleep-after") + 1])
        except (IndexError, ValueError):
            print("!! --sleep-after ждёт число секунд; беру значение из конфига")
    sleep_after_s = sleep_after_seconds({"sleep_after_s": sleep_after_s},
                                        default=DEFAULT_SLEEP_AFTER_S)
    device_note = mic.resolve_device()[1]
    choice = tts.current_choice()
    print(f"Голос: {choice['provider']} / {choice['voice']}; микрофон: {device_note}")
    print("Джарвис готов." + (f" Ctrl+Alt+J — включить микрофон; пауза "
                              f"{sleep_after_s:.0f} с — засыпаю." if continuous
                              else " Ctrl+Alt+J — говорите.") + " Ctrl+C — выход.")
    indicator = Indicator()
    indicator.start()
    indicator.set("sleeping")
    threading.Thread(target=hotkey_listener, daemon=True, name="jarvis-hotkey").start()
    time.sleep(1.5)  # плашка и hotkey успевают встать
    try:
        worker(assistant, indicator, sleep_after_s, continuous)
    except KeyboardInterrupt:
        print("\nДжарвис выключен.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

