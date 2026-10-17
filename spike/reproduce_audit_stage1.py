"""Воспроизведение дефектов из отчёта аудита 09_STAGE1_AUDIT.md.

Каждое утверждение проверяется кодом, а не чтением: если дефект не воспроизводится —
он не дефект; если воспроизводится — здесь же фиксируется наблюдаемое поведение,
которое станет регрессионным тестом.
"""

from __future__ import annotations

import ctypes
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_hands.events import FileWatcher
from mcp_hands.geometry import CoordinateSpace
from mcp_hands.input import INPUT_KEYBOARD, KEYEVENTF_UNICODE, VK_MENU, Actuator
from mcp_hands.screen import Frame, ScreenGrabber

results: list[tuple[str, str, str]] = []


def record(name: str, verdict: str, detail: str) -> None:
    results.append((name, verdict, detail))
    print(f"[{verdict}] {name}: {detail}")


def check_hotkey_vk() -> None:
    """P0-1: виртуальная клавиша попадает в wScan, а не в wVk."""
    actuator = Actuator()
    captured: list[tuple[int, int, int]] = []

    original = actuator.user32.SendInput

    def fake_send(count, pointer, size):
        for index in range(count):
            event = pointer[index]
            if event.type == INPUT_KEYBOARD:
                captured.append((event.ki.wVk, event.ki.wScan, event.ki.dwFlags))
        return count

    actuator.user32.SendInput = fake_send
    try:
        actuator.hotkey(VK_MENU)
    finally:
        actuator.user32.SendInput = original

    if not captured:
        record("P0-1 хоткей", "ИНАЧЕ", "события не отправлены вовсе")
        return
    vk, scan, flags = captured[0]
    if vk == VK_MENU:
        record("P0-1 хоткей", "ОК", f"wVk={vk} корректен")
    else:
        record("P0-1 хоткей", "ПОДТВЕРЖДЁН",
               f"wVk={vk} (ожидался {VK_MENU}), wScan={scan}, flags={hex(flags)} — "
               f"Windows проигнорирует событие")


def check_black_frame() -> None:
    """P1: проверка чёрного кадра не срабатывает из-за альфа-канала."""
    grabber = ScreenGrabber()
    black = np.zeros((64, 64, 4), dtype=np.uint8)
    black[:, :, 3] = 255
    issues = grabber._check(black)
    if "чёрный кадр" in issues:
        record("P1 чёрный кадр", "ОК", "чёрный кадр распознан")
    else:
        record("P1 чёрный кадр", "ПОДТВЕРЖДЁН",
               f"чёрный кадр не распознан (max={int(black.max())} по всем каналам): {issues}")


def check_normalized_clamp() -> None:
    """P1: нормализация не ограничивает диапазон 0..65535."""
    space = CoordinateSpace()
    rect = space.virtual_rect
    nx, ny = space.to_normalized(rect["right"] + 50, rect["bottom"] + 50)
    if nx <= 65535 and ny <= 65535:
        record("P1 нормализация", "ОК", f"значения в диапазоне: {nx}, {ny}")
    else:
        record("P1 нормализация", "ПОДТВЕРЖДЁН",
               f"выход за диапазон: {nx}, {ny} — SendInput переполнит 16-битный регистр")


def check_emoji_typing() -> None:
    """P1: символы вне BMP роняют ввод текста."""
    actuator = Actuator()
    try:
        actuator.type_text("🚀")
        record("P1 эмодзи", "ОК", "символ вне BMP отправлен без ошибки")
    except Exception as exc:  # noqa: BLE001
        record("P1 эмодзи", "ПОДТВЕРЖДЁН", f"{type(exc).__name__}: {exc}")


def check_crop_negative() -> None:
    """P1: кроп окна на мониторе левее основного."""
    grabber = ScreenGrabber()
    # Кадр всего виртуального экрана: он начинается левее основного монитора.
    frame = Frame(image=np.zeros((1080, 3840, 4), dtype=np.uint8), source="test", ms=1.0,
                  origin=(-1920, 0))
    rect = {"left": -1920, "top": 100, "right": -1000, "bottom": 800}
    try:
        crop = grabber.crop(rect, frame)
        inside = grabber.covers(frame, rect)
        record("P1 кроп с отрицательными координатами", "ОК",
               f"кроп {crop.size}, начало {crop.origin}, покрытие кадром {inside}")
    except Exception as exc:  # noqa: BLE001
        record("P1 кроп с отрицательными координатами", "ПОДТВЕРЖДЁН",
               f"{type(exc).__name__}: {exc}")
    # Кадр первичного монитора не покрывает окно слева — это обязано быть видно заранее.
    primary = Frame(image=np.zeros((1080, 1920, 4), dtype=np.uint8), source="dxgi", ms=1.0)
    if grabber.covers(primary, rect):
        record("P1 покрытие кадром монитора", "ИНАЧЕ", "первичный кадр «покрыл» чужой монитор")
    else:
        record("P1 покрытие кадром монитора", "ОК",
               "кадр первичного монитора честно не покрывает область на другом мониторе")


def check_file_overwrite() -> None:
    """P1: наблюдатель не видит перезапись существующего файла."""
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "existing.txt"
        target.write_text("v1", encoding="utf-8")
        watcher = FileWatcher(directory)
        watcher.start()
        try:
            time.sleep(0.3)
            target.write_text("v2 обновлено", encoding="utf-8")
            started = time.perf_counter()
            name = watcher.wait_for("existing.txt", timeout_s=1.5)
            elapsed = (time.perf_counter() - started) * 1000
        finally:
            watcher.stop()
    if name:
        record("P1 перезапись файла", "ОК", f"обнаружена за {elapsed:.0f} мс")
    else:
        record("P1 перезапись файла", "ПОДТВЕРЖДЁН",
               "перезапись существующего файла не обнаружена за 1.5 с")


def check_mss_failure_handled() -> None:
    """P0-3: отказ резервного пути захвата роняет агента вместо понятной ошибки."""
    grabber = ScreenGrabber()
    grabber._dxgi_error = "DXGI_FAILED (имитация)"

    def fail_mss():
        raise RuntimeError("BitBlt failed (имитация)")

    grabber._grab_mss = fail_mss
    try:
        grabber.grab()
        record("P0-3 отказ обоих путей захвата", "ОК", "вызов вернулся")
    except Exception as exc:  # noqa: BLE001
        kind = type(exc).__name__
        verdict = "ОК" if kind == "ScreenCaptureError" else "ПОДТВЕРЖДЁН"
        record("P0-3 отказ обоих путей захвата", verdict,
               f"исключение {kind}: {exc}")


def check_cursor_zero_bypass() -> None:
    """P0-4: при недоступном курсоре (0,0) клик отменяется."""
    actuator = Actuator()
    original_cursor = actuator.cursor

    def zero_cursor():
        return (0, 0)

    actuator.cursor = zero_cursor
    try:
        result = actuator.click(500, 500, verify=True)
    finally:
        actuator.cursor = original_cursor
    if result.get("ok"):
        record("P0-4 курсор (0,0)", "ОК", f"клик выполнен: {result}")
    else:
        record("P0-4 курсор (0,0)", "ПОДТВЕРЖДЁН",
               f"клик отменён: {result.get('reason')}")


if __name__ == "__main__":
    check_hotkey_vk()
    check_black_frame()
    check_normalized_clamp()
    check_emoji_typing()
    check_crop_negative()
    check_file_overwrite()
    check_mss_failure_handled()
    check_cursor_zero_bypass()
    print()
    print("Итого:")
    for name, verdict, _detail in results:
        print(f"  {verdict:<12} {name}")
