"""Фикстура «зависшее окно»: после --responsive-sec поток окна перестаёт разбирать сообщения.

Нужна, чтобы детерминированно проверить поведение UIA-зонда на неотвечающем приложении —
класс сбоя, из-за которого в DESIGN.md §4.2 появились процесс-зонд и IPC-таймаут 500 мс:
чужой поток висит в COM-вызове, и снять его можно только убийством процесса.

Печатает HWND/PID/TITLE одной строкой каждое, затем FREEZE в момент заморозки.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import tkinter


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Фикстура зависшего окна")
    parser.add_argument("--title", default="MCP-Hands hang fixture")
    parser.add_argument("--responsive-sec", type=float, default=0.5,
                        help="сколько окно отвечает до заморозки")
    parser.add_argument("--freeze-sec", type=float, default=120.0, help="длительность заморозки")
    parser.add_argument("--x", type=int, default=60)
    parser.add_argument("--y", type=int, default=60)
    parser.add_argument("--width", type=int, default=420)
    parser.add_argument("--height", type=int, default=220)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = tkinter.Tk()
    root.title(args.title)
    root.geometry(f"{args.width}x{args.height}+{args.x}+{args.y}")
    tkinter.Label(root, text="MCP-Hands: окно, которое перестанет отвечать").pack(padx=16, pady=16)
    tkinter.Label(root, text=f"заморозка через {args.responsive_sec:.1f} с").pack()
    root.update_idletasks()
    root.update()

    hwnd = _frame_hwnd(root)
    print(f"HWND={hwnd:#x}", flush=True)
    print(f"CLIENT_HWND={root.winfo_id():#x}", flush=True)
    print(f"PID={os.getpid()}", flush=True)
    print(f"TITLE={args.title}", flush=True)

    freeze_at = time.monotonic() + args.responsive_sec

    def tick() -> None:
        if time.monotonic() >= freeze_at:
            print("FREEZE", flush=True)
            time.sleep(args.freeze_sec)  # UI-поток не разбирает сообщения — окно «Не отвечает»
            root.destroy()
            return
        root.after(25, tick)

    root.after(25, tick)
    root.mainloop()
    print("EXIT", flush=True)
    return 0


def _frame_hwnd(root: tkinter.Tk) -> int:
    """HWND рамки окна (то, что видит UIA верхнего уровня)."""
    try:
        frame = root.wm_frame()
        if frame and int(frame, 16) != 0:
            return int(frame, 16)
    except (tkinter.TclError, ValueError):
        pass
    return root.winfo_id()


if __name__ == "__main__":
    sys.exit(main())
