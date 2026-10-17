"""Разведка: как Windows 11 Блокнот показывает диалог сохранения.

От этого зависит сценарий: классический Win32-диалог (`#32770`, отдельное окно) или
XAML-диалог внутри того же окна. Проверяем фактами: ждём событие появления окна,
смотрим класс, дерево элементов и наличие кнопки сохранения.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_hands.events import EVENT_OBJECT_SHOW, EVENT_SYSTEM_FOREGROUND, EventPump
from mcp_hands.geometry import CoordinateSpace, enable_dpi_awareness
from mcp_hands.input import VK_CONTROL, VK_S, Actuator
from mcp_hands.uia import UiaClient, child_windows, enum_windows


def main() -> int:
    enable_dpi_awareness()
    space = CoordinateSpace()
    actuator = Actuator(space)
    client = UiaClient()

    before = {window["hwnd"] for window in enum_windows() if window["exe"].lower() == "notepad.exe"}
    process = subprocess.Popen(["notepad.exe"])
    print("запущен Блокнот, pid:", process.pid)

    with EventPump(events=(EVENT_SYSTEM_FOREGROUND, EVENT_OBJECT_SHOW)) as pump:
        event = pump.wait(lambda e: "Блокнот" in e.title or "Notepad" in e.title, timeout_s=15)
        print("событие окна:", event.describe() if event else "нет")
        time.sleep(0.8)
        windows = [w for w in enum_windows() if w["exe"].lower() == "notepad.exe"]
        print("окна Блокнота:", [(hex(w["hwnd"]), w["title"][:40]) for w in windows])
        if not windows:
            return 1
        hwnd = windows[0]["hwnd"]
        after = {w["hwnd"] for w in windows}
        print("новых окон:", [hex(h) for h in after - before])

        print("активация:", actuator.activate(hwnd))
        time.sleep(0.4)
        actuator.type_text("Проверка сохранения")
        time.sleep(0.5)

        print("\n--- нажимаю Ctrl+S ---")
        pump.drain()
        actuator.hotkey(VK_CONTROL, VK_S)
        dialog_event = pump.wait(lambda e: e.hwnd != hwnd and e.process_id == process.pid,
                                 timeout_s=8)
        print("событие диалога:", dialog_event.describe() if dialog_event else "нет события о новом окне")

        time.sleep(0.8)
        windows_now = [w for w in enum_windows() if w["pid"] == process.pid]
        print("окна процесса Блокнота:", [(hex(w["hwnd"]), w["title"][:50]) for w in windows_now])
        children = child_windows(hwnd)
        print("дочерние HWND окна:", [(hex(c['hwnd']), c["class_name"]) for c in children if c["visible"]][:6])

        root, info = client.content_root(hwnd, jump=True)
        print("корень контента:", info)
        for control_type in ("edit", "button"):
            node = client.find(root, control_type=control_type)
            print(f"  первый {control_type}:", {k: node[k] for k in ("name", "rect")} if node else "нет")

        print("\n--- закрываю Блокнот без сохранения ---")
        actuator.activate(hwnd)
        actuator.hotkey(VK_CONTROL, 0x51)  # Ctrl+Q не нужен; закрываем процессом
    process.kill()
    process.wait(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
