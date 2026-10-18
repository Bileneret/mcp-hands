"""Панель Джарвиса: настройка и проверка в одном окне (tkinter).

Слева — микрофон (список, тест уровня, сохранение выбора), справа — голос
(провайдер, голоса, прослушивание, скорость/тон, сохранение). Внизу — быстрые
проверки (распознавание речи, статус живой Opera) и запуск самого Джарвиса.

Интерфейс не блокируется: запись, синтез и сетевые списки голосов идут в
рабочих потоках, а результат возвращается в окно через очередь.

Запуск: .venv\\Scripts\\python.exe -u tools\\jarvis_panel.py   (или jarvis_panel.bat)
Проверка сборки без дисплея: ... --smoke  (создаёт окно и закрывает через 2 с)
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcp_hands.assistant import mic, tts  # noqa: E402

DEMO_PHRASE = "Джарвис на связи. Включаю свежие новости и разворачиваю на весь экран."


class Panel(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Джарвис — настройка и проверка")
        self.geometry("860x560")
        self.results: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._build()
        self.after(100, self._drain)
        self._refresh_mics()
        self._refresh_voices()
        self._show_saved()

    # --- построение интерфейса -------------------------------------------

    def _build(self) -> None:
        header = ttk.Label(self, text="Джарвис: послушать, выбрать, проверить",
                           font=("Segoe UI", 13, "bold"))
        header.pack(pady=(10, 4))
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=12)

        # --- микрофон
        mic_frame = ttk.LabelFrame(body, text="Микрофон")
        mic_frame.pack(side="left", fill="both", expand=True, padx=(0, 6))
        self.mic_list = tk.Listbox(mic_frame, height=12)
        self.mic_list.pack(fill="both", expand=True, padx=8, pady=6)
        mic_buttons = ttk.Frame(mic_frame)
        mic_buttons.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Button(mic_buttons, text="Обновить список",
                   command=self._refresh_mics).pack(side="left")
        ttk.Button(mic_buttons, text="Тест записи (3 с)",
                   command=self._test_mic).pack(side="left", padx=6)
        ttk.Button(mic_buttons, text="Сохранить выбор",
                   command=self._save_mic).pack(side="left")
        self.mic_status = ttk.Label(mic_frame, text="", wraplength=380)
        self.mic_status.pack(fill="x", padx=8, pady=(0, 8))

        # --- голос
        voice_frame = ttk.LabelFrame(body, text="Голос")
        voice_frame.pack(side="left", fill="both", expand=True, padx=(6, 0))
        provider_row = ttk.Frame(voice_frame)
        provider_row.pack(fill="x", padx=8, pady=6)
        ttk.Label(provider_row, text="Провайдер:").pack(side="left")
        self.provider = ttk.Combobox(provider_row, state="readonly",
                                     values=["piper — локальный нейро",
                                             "edge — нейро Microsoft (онлайн)",
                                             "windows — системный"])
        self.provider.current(0)
        self.provider.bind("<<ComboboxSelected>>", lambda _e: self._refresh_voices())
        self.provider.pack(side="left", padx=6, fill="x", expand=True)
        self.voice_list = tk.Listbox(voice_frame, height=8)
        self.voice_list.pack(fill="both", expand=True, padx=8)
        phrase_row = ttk.Frame(voice_frame)
        phrase_row.pack(fill="x", padx=8, pady=(6, 2))
        ttk.Label(phrase_row, text="Фраза:").pack(side="left")
        self.phrase = ttk.Entry(phrase_row)
        self.phrase.insert(0, DEMO_PHRASE)
        self.phrase.pack(side="left", fill="x", expand=True, padx=6)
        tuning_row = ttk.Frame(voice_frame)
        tuning_row.pack(fill="x", padx=8)
        ttk.Label(tuning_row, text="Скорость (edge):").pack(side="left")
        self.rate = ttk.Entry(tuning_row, width=7)
        self.rate.insert(0, "+0%")
        self.rate.pack(side="left", padx=4)
        ttk.Label(tuning_row, text="Тон:").pack(side="left")
        self.pitch = ttk.Entry(tuning_row, width=7)
        self.pitch.insert(0, "+0Hz")
        self.pitch.pack(side="left", padx=4)
        voice_buttons = ttk.Frame(voice_frame)
        voice_buttons.pack(fill="x", padx=8, pady=6)
        ttk.Button(voice_buttons, text="▶ Прослушать выбранный",
                   command=self._listen).pack(side="left")
        ttk.Button(voice_buttons, text="▶ Прослушать ВСЕ подряд",
                   command=self._listen_all).pack(side="left", padx=6)
        ttk.Button(voice_buttons, text="Сохранить голос",
                   command=self._save_voice).pack(side="left")
        self.voice_status = ttk.Label(voice_frame, text="", wraplength=400)
        self.voice_status.pack(fill="x", padx=8, pady=(0, 8))

        # --- проверки и запуск
        checks = ttk.LabelFrame(self, text="Проверки и запуск")
        checks.pack(fill="x", padx=12, pady=10)
        ttk.Button(checks, text="Проверить распознавание (скажи фразу)",
                   command=self._test_stt).pack(side="left", padx=8, pady=6)
        ttk.Button(checks, text="Статус живой Opera",
                   command=self._test_live).pack(side="left", padx=6)
        ttk.Button(checks, text="Запустить Джарвиса (Ctrl+Alt+J)",
                   command=self._start_jarvis).pack(side="left", padx=6)
        self.checks_status = ttk.Label(self, text="", wraplength=820)
        self.checks_status.pack(fill="x", padx=14, pady=(0, 8))

    # --- потоки и очередь -------------------------------------------------

    def _run(self, key: str, work) -> None:
        """Выполняет работу в потоке; результат придёт в _drain по ключу."""
        def target() -> None:
            try:
                self.results.put((key, work()))
            except Exception as exc:  # noqa: BLE001 — ошибка едет в интерфейс
                self.results.put((key, {"error": f"{type(exc).__name__}: {exc}"}))
        threading.Thread(target=target, daemon=True).start()

    def _drain(self) -> None:
        try:
            while True:
                key, payload = self.results.get_nowait()
                self._apply(key, payload)
        except queue.Empty:
            pass
        self.after(100, self._drain)

    def _apply(self, key: str, payload) -> None:
        if key == "mics":
            self.mic_devices = payload
            self.mic_list.delete(0, "end")
            saved = mic.current_device_name()
            for device in payload:
                mark = " ★" if device["is_default"] else ""
                star = " ✔" if device["name"] == saved else ""
                self.mic_list.insert("end", f"{device['index']:>3}  {device['name']}{mark}{star}")
            self.mic_status.configure(text="Список обновлён. Выбери устройство и сохрани.")
        elif key == "mic_test":
            if "error" in payload:
                self.mic_status.configure(text=f"Ошибка теста: {payload['error']}")
            else:
                verdict = "СИГНАЛ ЕСТЬ" if payload["has_signal"] else "тишина — проверь выбор"
                self.mic_status.configure(text=f"Уровень {payload['level']} — {verdict}")
        elif key == "voices":
            self.voice_items = payload
            self.voice_list.delete(0, "end")
            for item in payload:
                self.voice_list.insert("end", item["label"])
        elif key == "hear":
            self.voice_status.configure(text=payload.get("note", ""))
        elif key == "stt":
            if "error" in payload:
                self.checks_status.configure(text=f"Ошибка распознавания: {payload['error']}")
            else:
                self.checks_status.configure(
                    text=f"Услышал: {payload['text']!r} ({payload['ms']} мс)")
        elif key == "live":
            self.checks_status.configure(text=payload.get("note", ""))
        elif key == "saved":
            self.checks_status.configure(text=payload.get("note", ""))

    # --- микрофон ---------------------------------------------------------

    def _refresh_mics(self) -> None:
        self._run("mics", mic.list_input_devices)

    def _selected_mic(self) -> dict | None:
        selection = self.mic_list.curselection()
        if not selection:
            return None
        return self.mic_devices[selection[0]]

    def _test_mic(self) -> None:
        device = self._selected_mic() or {}
        index = device.get("index", None)
        self.mic_status.configure(text="Говори 3 секунды…")
        self._run("mic_test", lambda: mic.test_input(3.0, device=index))

    def _save_mic(self) -> None:
        device = self._selected_mic()
        if device is None:
            messagebox.showinfo("Микрофон", "Сначала выбери устройство в списке.")
            return
        mic.set_device(device["name"])
        self.mic_status.configure(text=f"Сохранено: {device['name']}")
        self._refresh_mics()

    # --- голос ------------------------------------------------------------

    def _provider_id(self) -> str:
        return self.provider.get().split(" — ")[0].strip()

    def _refresh_voices(self) -> None:
        provider = self._provider_id()
        self.voice_status.configure(text=f"Загружаю голоса {provider}…")
        self._run("voices", lambda: tts.list_voices(provider))

    def _current_voice(self) -> str:
        selection = self.voice_list.curselection()
        if not selection:
            return ""
        return self.voice_items[selection[0]]["id"]

    def _listen(self, voice: str | None = None, note: str = "") -> None:
        voice = voice or self._current_voice()
        if not voice:
            messagebox.showinfo("Голос", "Выбери голос в списке.")
            return
        provider, text = self._provider_id(), self.phrase.get() or DEMO_PHRASE
        rate, pitch = self.rate.get(), self.pitch.get()
        self.voice_status.configure(text=note or f"Слушай: {voice}…")
        self._run("hear", lambda: (tts.speak_with(provider, voice, text, rate, pitch),
                                   {"note": f"Прослушано: {voice}"})[1])

    def _listen_all(self) -> None:
        items = list(getattr(self, "voice_items", []))
        if not items:
            return

        def work() -> dict:
            for index, item in enumerate(items, 1):
                tts.speak_with(self._provider_id(), item["id"], self.phrase.get() or DEMO_PHRASE,
                               self.rate.get(), self.pitch.get())
            return {"note": f"Прослушаны все {len(items)} голосов — выбери лучший и сохрани."}
        self.voice_status.configure(text="Слушаю все голоса подряд…")
        self._run("hear", work)

    def _save_voice(self) -> None:
        voice = self._current_voice()
        if not voice:
            messagebox.showinfo("Голос", "Выбери голос в списке.")
            return
        tts.save_config({"tts": {"provider": self._provider_id(), "voice": voice,
                                 "rate": self.rate.get(), "pitch": self.pitch.get()}})
        self.voice_status.configure(text=f"Сохранено: {self._provider_id()} / {voice}")

    def _show_saved(self) -> None:
        choice = tts.current_choice()
        self.checks_status.configure(
            text=f"Сейчас сохранено: голос {choice['provider']} / {choice['voice']}; "
                 f"микрофон {mic.current_device_name() or 'системный'}")

    # --- проверки ---------------------------------------------------------

    def _test_stt(self) -> None:
        from mcp_hands.assistant.voice import record_until_silence, transcribe

        self.checks_status.configure(text="Говори фразу (запись до паузы)…")

        def work() -> dict:
            audio = record_until_silence(device=mic.resolve_device()[0])
            if audio.size == 0:
                return {"error": "ничего не записалось (тишина или микрофон не выбран)"}
            heard = transcribe(audio)
            return {"text": heard["text"], "ms": heard["ms"]}
        self._run("stt", work)

    def _test_live(self) -> None:
        from mcp_hands.live import LiveBrowser

        def work() -> dict:
            browser = LiveBrowser()
            outcome = browser.connect_user()
            if not outcome.get("ok"):
                return {"note": "Живая Opera недоступна: запусти tools/opera_debug_launcher.py"}
            tabs = browser.cdp.endpoint("/json/list")
            pages = [tab for tab in tabs if tab.get("type") == "page"]
            return {"note": f"Живая Opera на связи (порт {outcome['port']}): "
                            f"{len(pages)} вкладок"}
        self.checks_status.configure(text="Проверяю живую Opera…")
        self._run("live", work)

    def _start_jarvis(self) -> None:
        subprocess.Popen([sys.executable, "-u", str(ROOT / "tools" / "jarvis.py")],
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
        self._run("saved", lambda: {"note": "Джарвис запущен в отдельном окне: Ctrl+Alt+J — говорить."})


def main() -> int:
    panel = Panel()
    if "--smoke" in sys.argv:  # проверка сборки без человека
        panel.after(2000, panel.destroy)
    panel.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
