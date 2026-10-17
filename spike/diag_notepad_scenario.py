"""Диагностика: почему в сценарии текст не доходит, хотя отдельный опыт работает.

Повторяет последовательность сценария шаг за шагом и печатает состояние: активное окно,
фокус, текст документа, координаты вкладки.
"""

from __future__ import annotations

import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_hands.geometry import CoordinateSpace, enable_dpi_awareness
from mcp_hands.input import Actuator
from mcp_hands.scenarios.notepad import find_our_window, find_tab, tab_names
from mcp_hands.uia import UiaClient, element_text

TEXT = "Проверка: строка первая\n"


def state(uia: UiaClient, hwnd: int, label: str) -> None:
    root, _info = uia.content_root(hwnd, jump=True)
    try:
        focus = bool(root.CurrentHasKeyboardFocus)
    except Exception as exc:  # noqa: BLE001
        focus = f"ошибка {exc}"
    tabs = tab_names(uia, hwnd)
    print(f"  [{label}] текст={element_text(root)[:40]!r} | фокус={focus} | вкладок={len(tabs)}")


def main() -> int:
    enable_dpi_awareness()
    actuator = Actuator(CoordinateSpace())
    uia = UiaClient()

    workdir = Path(__file__).resolve().parent / ".notepad_diag"
    workdir.mkdir(exist_ok=True)
    marker = f"mcp-hands-{uuid.uuid4().hex[:10]}"
    target = workdir / f"{marker}.txt"
    target.write_text("", encoding="utf-8")

    subprocess.Popen(["notepad.exe", str(target)])
    window = find_our_window(marker, timeout_s=20)
    print("окно:", window.get("title"), "| pid:", window.get("pid"))
    if not window:
        return 1
    hwnd = window["hwnd"]

    print("1) активация:", actuator.activate(hwnd))
    time.sleep(0.4)
    state(uia, hwnd, "после активации")

    tab = find_tab(uia, hwnd, marker)
    print("2) вкладка:", {k: tab.get(k) for k in ("name", "rect", "center")} if tab else "не найдена")
    if tab and tab.get("center"):
        print("   клик по вкладке:", actuator.click(*tab["center"], verify=True))
        time.sleep(0.4)
        state(uia, hwnd, "после клика по вкладке")

    root, _info = uia.content_root(hwnd, jump=True)
    try:
        root.SetFocus()
        focus_after = root.CurrentHasKeyboardFocus
    except Exception as exc:  # noqa: BLE001
        focus_after = f"исключение {exc}"
    time.sleep(0.3)
    print("3) SetFocus на документ ->", focus_after)
    print("   активное окно:", hex(actuator.foreground()), "| наше:", hex(hwnd))
    state(uia, hwnd, "перед вводом")

    print("4) ввод:", actuator.type_text(TEXT, char_delay_s=0.012))
    time.sleep(0.6)
    state(uia, hwnd, "после ввода")

    actuator.hotkey(0x11, 0x53)
    time.sleep(1.2)
    print("5) файл:", repr(target.read_text(encoding="utf-8", errors="replace")[:60]),
          "| байт:", target.stat().st_size)

    # закрываем только нашу вкладку
    tab = find_tab(uia, hwnd, marker)
    if tab and tab.get("center"):
        actuator.click(*tab["center"], verify=True)
        time.sleep(0.3)
        actuator.hotkey(0x11, 0x57)
        time.sleep(0.6)
    print("   вкладок после закрытия:", tab_names(uia, hwnd))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
