"""Разведка ввода в Блокнот: куда уходит текст и как прочитать содержимое документа.

Проверяем: нужен ли явный SetFocus на элемент документа (окно можно активировать,
а фокус останется на вкладке), и как получить текст — дети в WinUI-Блокноте не отдаются,
значит нужен TextPattern.
"""

from __future__ import annotations

import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from comtypes.gen import UIAutomationClient as UIA  # noqa: E402

from mcp_hands.geometry import CoordinateSpace, enable_dpi_awareness  # noqa: E402
from mcp_hands.input import Actuator  # noqa: E402
from mcp_hands.scenarios.notepad import find_our_window, launch_with_file  # noqa: E402
from mcp_hands.uia import UiaClient  # noqa: E402

TEXT = "Проверка ввода: строка первая\n"


def document_element(uia: UiaClient, hwnd: int):
    """Элемент документа.

    Корень контента у Блокнота сам является документом (прыжок к контенту нашёл
    `Document`), поэтому искать документ среди его потомков бессмысленно — берём корень.
    """
    root, info = uia.content_root(hwnd, jump=True)
    print("корень контента:", info)
    if info.get("how") == "document":
        return root
    condition = uia.automation.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId,
                                                       UIA.UIA_DocumentControlTypeId)
    return uia.window(hwnd).FindFirst(UIA.TreeScope_Descendants, condition)


def read_text(element) -> str:
    """Текст документа через TextPattern: в WinUI он не отдаётся детьми."""
    try:
        pattern = element.GetCurrentPattern(UIA.UIA_TextPatternId)
    except Exception as exc:  # noqa: BLE001
        return f"<нет TextPattern: {exc}>"
    if not pattern:
        return "<TextPattern недоступен>"
    try:
        text_pattern = pattern.QueryInterface(UIA.IUIAutomationTextPattern)
        return text_pattern.DocumentRange.GetText(-1)
    except Exception as exc:  # noqa: BLE001
        return f"<ошибка чтения: {exc}>"


def main() -> int:
    enable_dpi_awareness()
    space = CoordinateSpace()
    actuator = Actuator(space)
    uia = UiaClient()

    workdir = Path(__file__).resolve().parent / ".notepad_diag"
    workdir.mkdir(exist_ok=True)
    marker = f"mcp-hands-{uuid.uuid4().hex[:10]}"
    target = workdir / f"{marker}.txt"
    target.write_text("", encoding="utf-8")

    process = launch_with_file(target)
    window = find_our_window(marker, timeout_s=20)
    print("окно:", window.get("title"), "| pid:", window.get("pid"))
    if not window:
        process.kill()
        return 1
    hwnd = window["hwnd"]
    print("активация:", actuator.activate(hwnd))
    time.sleep(0.5)

    element = document_element(uia, hwnd)
    if element is None:
        process.kill()
        return 1
    print("текст до ввода:", repr(read_text(element)[:60]))
    print("фокус до ввода:", element.CurrentHasKeyboardFocus)

    try:
        element.SetFocus()
        time.sleep(0.3)
        print("фокус после SetFocus:", element.CurrentHasKeyboardFocus)
    except Exception as exc:  # noqa: BLE001
        print("SetFocus не удался:", exc)

    print("ввод текста:", actuator.type_text(TEXT))
    time.sleep(0.8)
    print("текст после ввода:", repr(read_text(element)[:80]))
    print("заголовок окна:", find_our_window(marker, timeout_s=1).get("title"))

    actuator.hotkey(0x11, 0x53)  # Ctrl+S
    time.sleep(1.5)
    print("файл на диске:", repr(target.read_text(encoding='utf-8', errors='replace')[:80]),
          "| байт:", target.stat().st_size)

    if find_our_window(marker, timeout_s=1):
        subprocess.run(["taskkill", "/F", "/PID", str(find_our_window(marker, timeout_s=1)["pid"])],
                       capture_output=True)
    process.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
