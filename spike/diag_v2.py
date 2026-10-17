"""Диагностика после правок аудита v2: почему живой клик и OCR ведут себя иначе.

Запускается файлом (не через stdin): сателлит использует `multiprocessing`, которому
нужен настоящий модуль `__main__`, а не поток ввода.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_hands.geometry import CoordinateSpace, enable_dpi_awareness
from mcp_hands.input import Actuator
from mcp_hands.ocr import WindowsOcr
from mcp_hands.probe import ProbeProcess
from mcp_hands.screen import ScreenGrabber
from mcp_hands.uia import find_window


def main() -> int:
    enable_dpi_awareness()
    space = CoordinateSpace()
    grabber = ScreenGrabber()
    window = find_window(title_contains="diag target")
    if not window:
        print("мишень не найдена")
        return 1
    hwnd = window["hwnd"]

    actuator = Actuator()
    print("рабочий стол ввода:", actuator.attach_to_input_desktop())
    print("активация окна:", actuator.activate(hwnd))
    time.sleep(0.4)

    frame = grabber.grab()
    rect = space.window_frame(hwnd)
    print(f"кадр {frame.size} начало {frame.origin} | рамка окна {rect}")
    crop = grabber.crop(rect, frame)
    crop.to_rgb().save(str(Path(__file__).resolve().parent / ".diag_crop.png"))

    ocr_words = WindowsOcr().recognize(crop.image, offset=(rect["left"], rect["top"]))
    print("слова OCR:", [(word.text, word.center) for word in ocr_words])

    with ProbeProcess(default_timeout_s=5.0) as satellite:
        print("сателлит:", satellite.ping().get("desktop"))
        result = satellite.click(hwnd, name="alpha", control_type="button")
        print("клик alpha:", result)
    time.sleep(0.3)
    log = Path(__file__).resolve().parent / ".diag.log"
    print("журнал мишени:", log.read_text(encoding="utf-8").strip() or "<пусто>")

    # Та же операция, но с проверкой результата SendInput напрямую в этом процессе.
    actuator.cursor = lambda: (1, 1)  # имитация: позиция курсора не отдаётся
    print("клик вслепую (должен быть заблокирован):",
          actuator.click(500, 400, verify=True, settle_s=0, allow_unverified=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
