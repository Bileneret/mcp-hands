"""Индикатор ассистента: маленькая плашка в углу экрана (tkinter, без рамки).

Состояния: слушаю → думаю → делаю → говорю (и текст ответа на пару секунд).
Плашка не забирает фокус (`-topmost` без `focus_force`) и не мешает работе.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk

STATES = {
    "listening": ("🎙", "Слушаю…", "#1f6feb"),
    "thinking": ("⏳", "Думаю…", "#9e6a03"),
    "working": ("⚙️", "Делаю…", "#8250df"),
    "talking": ("🗣", "", "#1a7f37"),
    "sleeping": ("😴", "заснул: Ctrl+Alt+J", "#30363d"),
    "idle": ("🟢", "", "#30363d"),
}


class Indicator:
    """Потокобезопасная плашка: главный поток — tkinter, остальные кладут события."""

    def __init__(self) -> None:
        self.queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self.root: tk.Tk | None = None
        self.label: tk.Label | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="jarvis-overlay")
        self._thread.start()

    def set(self, state: str, text: str = "") -> None:
        self.queue.put((state, text))

    # --- внутреннее ---------------------------------------------------------

    def _run(self) -> None:
        root = tk.Tk()
        self.root = root
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        try:
            root.attributes("-alpha", 0.92)
        except tk.TclError:
            pass
        root.configure(bg="#161b22")
        self.label = tk.Label(root, text="🟢", bg="#161b22", fg="#e6edf3",
                              font=("Segoe UI", 12), padx=14, pady=8)
        self.label.pack()
        root.update_idletasks()
        width, height = root.winfo_width(), root.winfo_height()
        root.geometry(f"+{root.winfo_screenwidth() - width - 24}"
                      f"+{root.winfo_screenheight() - height - 80}")
        root.after(80, self._pump)
        root.mainloop()

    def _pump(self) -> None:
        try:
            while True:
                state, text = self.queue.get_nowait()
                icon, label, color = STATES.get(state, STATES["idle"])
                shown = text or label
                if self.label is not None:
                    self.label.configure(text=f"{icon}  {shown}" if shown else icon)
                    if self.root is not None:
                        self.root.configure(bg="#161b22")
        except queue.Empty:
            pass
        if self.root is not None:
            self.root.after(80, self._pump)
