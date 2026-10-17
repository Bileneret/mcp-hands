"""Диагностика проверяемого ввода в Блокнот: что видно после каждой попытки."""

from __future__ import annotations

import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_hands.geometry import CoordinateSpace, enable_dpi_awareness
from mcp_hands.input import Actuator
from mcp_hands.scenarios.notepad import find_our_window, launch_with_file
from mcp_hands.uia import UiaClient, element_text

TEXT = "Проверка ввода: строка первая\n"


def main() -> int:
    enable_dpi_awareness()
    actuator = Actuator(CoordinateSpace())
    uia = UiaClient()
    workdir = Path(__file__).resolve().parent / ".notepad_diag"
    workdir.mkdir(exist_ok=True)
    marker = f"mcp-hands-{uuid.uuid4().hex[:10]}"
    target = workdir / f"{marker}.txt"
    target.write_text("", encoding="utf-8")

    process = launch_with_file(target)
    started = time.perf_counter()
    window = find_our_window(marker, timeout_s=25)
    print(f"окно появилось за {time.perf_counter() - started:.1f} с:", window.get("title"))
    if not window:
        process.kill()
        return 1
    hwnd = window["hwnd"]
    print("активация:", actuator.activate(hwnd))
    time.sleep(0.5)

    root, info = uia.content_root(hwnd, jump=True)
    print("корень:", info, "| текст корня:", repr(element_text(root)[:40]))
    reader = lambda: element_text(root)

    for attempt in range(1, 4):
        print(f"\n--- попытка {attempt} ---")
        result = actuator.type_text(TEXT, char_delay_s=0.012)
        time.sleep(0.5)
        observed = reader()
        print("после ввода:", repr(observed[:60]), "| отправлено знаков:", result["chars"])
        if TEXT.strip() in observed:
            print("совпало")
            break
        print("очищаю поле: Ctrl+A, Delete")
        actuator.hotkey(0x11, 0x41)
        time.sleep(0.15)
        actuator._send((1, 0, 0, 0, 0x2E), (1, 0x0002, 0, 0, 0x2E))
        time.sleep(0.3)
        print("после очистки:", repr(reader()[:40]))

    actuator.hotkey(0x11, 0x53)
    time.sleep(1.2)
    print("\nфайл на диске:", repr(target.read_text(encoding="utf-8", errors="replace")[:60]))

    left = find_our_window(marker, timeout_s=1)
    if left:
        subprocess.run(["taskkill", "/F", "/PID", str(left["pid"])], capture_output=True)
    process.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
