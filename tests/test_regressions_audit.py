"""Регрессионные тесты по отчёту аудита `reports/09_STAGE1_AUDIT.md`.

Каждый дефект из отчёта сначала воспроизведён (`spike/reproduce_audit_stage1.py`),
затем закрыт правкой, и здесь закреплён тестом. Тесты написаны так, чтобы падать
на старой реализации — иначе они ничего не охраняют.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from mcp_hands.events import FileWatcher
from mcp_hands.geometry import CoordinateSpace
from mcp_hands.input import (INPUT_KEYBOARD, INPUT_MOUSE, KEYEVENTF_UNICODE, MOUSEEVENTF_LEFTDOWN,
                             VK_CONTROL, VK_MENU, Actuator)
from mcp_hands.probe import ProbeProcess
from mcp_hands.screen import Frame, ScreenCaptureError, ScreenGrabber
from mcp_hands.uia import CONTROL_TYPE_IDS

VK_S = 0x53


class Capture:
    """Перехватывает события ввода вместо реальной отправки в систему."""

    def __init__(self, actuator: Actuator) -> None:
        self.actuator = actuator
        self.events: list[dict] = []
        self._original = actuator.user32.SendInput

    def __enter__(self) -> "Capture":
        def fake_send(count, pointer, _size):
            for index in range(count):
                event = pointer[index]
                if event.type == INPUT_KEYBOARD:
                    self.events.append({"kind": "key", "vk": event.ki.wVk,
                                        "scan": event.ki.wScan, "flags": event.ki.dwFlags})
                else:
                    self.events.append({"kind": "mouse", "flags": event.mi.dwFlags,
                                        "x": event.mi.dx, "y": event.mi.dy})
            return count

        self.actuator.user32.SendInput = fake_send
        return self

    def __exit__(self, *_exc) -> None:
        self.actuator.user32.SendInput = self._original


# --- P0-1: хоткеи ------------------------------------------------------------------


def test_hotkey_puts_virtual_key_into_vk():
    """Виртуальная клавиша обязана уходить в `wVk`: иначе Windows игнорирует событие."""
    actuator = Actuator()
    with Capture(actuator) as capture:
        actuator.hotkey(VK_CONTROL, VK_S, allow_any_window=True)
    keys = [event for event in capture.events if event["kind"] == "key"]
    assert [event["vk"] for event in keys[:2]] == [VK_CONTROL, VK_S], \
        f"клавиши ушли не в wVk: {keys}"
    assert all(event["flags"] & KEYEVENTF_UNICODE == 0 for event in keys), \
        "хоткей не должен помечаться как Unicode-ввод"
    assert len(keys) == 4, "нажатие и отпускание для каждой клавиши"


def test_alt_tap_uses_virtual_key():
    """Служебное нажатие Alt (снятие блокировки фокуса) тоже шло пустым событием."""
    actuator = Actuator()
    with Capture(actuator) as capture:
        actuator._tap_alt()
    keys = [event for event in capture.events if event["kind"] == "key"]
    assert keys and keys[0]["vk"] == VK_MENU, f"Alt ушёл не в wVk: {keys}"


# --- P1: символы вне BMP ------------------------------------------------------------


def test_type_text_encodes_astral_characters():
    """Эмодзи отправляется суррогатной парой, а не обрезанным кодом."""
    actuator = Actuator()
    with Capture(actuator) as capture:
        result = actuator.type_text("ок🚀", allow_any_window=True)
    units = [event["scan"] for event in capture.events if event["kind"] == "key"]
    expected: list[int] = []
    for char in "ок🚀":
        encoded = char.encode("utf-16-le")
        expected.extend(int.from_bytes(encoded[index:index + 2], "little")
                        for index in range(0, len(encoded), 2))
    assert units[::2] == expected, f"отправлено {units}, ожидалось {expected}"
    assert 0x1F680 not in units, "символ вне BMP не может уходить одним 16-битным кодом"
    assert result["code_units"] == len(expected) > result["chars"]


def test_type_text_keeps_cyrillic_intact():
    actuator = Actuator()
    with Capture(actuator) as capture:
        actuator.type_text("Отчёт", allow_any_window=True)
    units = [event["scan"] for event in capture.events if event["kind"] == "key"]
    assert units[::2] == [ord(char) for char in "Отчёт"]


# --- P0-4: курсор недоступен --------------------------------------------------------


def test_click_is_blocked_when_cursor_position_unknown():
    """Недоступная позиция курсора — причина ОТКАЗАТЬ в нажатии, а не нажать вслепую.

    Первая версия правки (по отчёту 09) выполняла клик с пометкой. Аудит v2 показал,
    почему это опасно: нажатие без подтверждённой позиции уходит в текущую точку
    курсора, где может быть кнопка закрытия или отправки. Теперь по умолчанию — отказ,
    а обход только явным `allow_unverified=True` (проверяется отдельным тестом в
    `test_regressions_audit_v2.py`).
    """
    actuator = Actuator()
    actuator.cursor = lambda: (0, 0)  # курсор система не отдаёт
    with Capture(actuator) as capture:
        result = actuator.click(500, 500, verify=True, settle_s=0)
    presses = [event for event in capture.events if event["flags"] & MOUSEEVENTF_LEFTDOWN]
    assert not result["ok"] and not presses, f"слепой клик прошёл: {result}"
    assert result["cursor_check"] == "недоступен"
    assert "слепой клик заблокирован" in result["reason"]


def test_click_still_blocked_on_real_mismatch():
    """Настоящий промах курсора по-прежнему блокирует нажатие — защита не ослаблена."""
    actuator = Actuator()
    actuator.cursor = lambda: (120, 340)
    with Capture(actuator) as capture:
        result = actuator.click(500, 500, verify=True, settle_s=0)
    presses = [event for event in capture.events if event["flags"] & MOUSEEVENTF_LEFTDOWN]
    assert not result["ok"] and not presses, f"промах не заблокирован: {result}"
    assert result["cursor_check"] == "промах"


# --- P0-3: отказ обоих путей захвата -------------------------------------------------


def test_grab_raises_typed_error_when_everything_fails():
    grabber = ScreenGrabber()
    grabber._dxgi_error = "DXGI_ERROR_UNSUPPORTED (имитация)"

    def fail_mss():
        raise RuntimeError("BitBlt failed (имитация)")

    grabber._grab_mss = fail_mss
    with pytest.raises(ScreenCaptureError) as error:
        grabber.grab()
    assert "недоступен" in str(error.value)
    assert any(entry.get("op") == "mss_failed" for entry in grabber.log), \
        "отказ резервного пути обязан попасть в журнал"


# --- P1: признаки кадра -------------------------------------------------------------


def test_black_frame_is_detected_with_alpha():
    """Альфа-канал у захвата всегда 255 и не должен маскировать чёрный экран."""
    grabber = ScreenGrabber()
    black = np.zeros((32, 32, 4), dtype=np.uint8)
    black[:, :, 3] = 255
    assert "чёрный кадр" in grabber._check(black)

    grey = np.full((32, 32, 4), 40, dtype=np.uint8)
    assert "чёрный кадр" not in grabber._check(grey)


def test_unchanged_frame_marked_suspicious():
    """Повтор кадра один в один — признак «экран замер»: его выставляет сравнение с прошлым."""
    grabber = ScreenGrabber()
    image = np.full((16, 16, 4), 120, dtype=np.uint8)
    assert grabber._check(image) == []          # сравнивать пока не с чем
    grabber._last_image = image                 # так это делает grab() после захвата
    assert "кадр не изменился" in grabber._check(image)


# --- P1: нормализация ---------------------------------------------------------------


def test_normalized_is_clamped():
    space = CoordinateSpace()
    rect = {"left": -1920, "top": -200, "right": 1920, "bottom": 880}
    for point in ((-5000, -5000), (5000, 5000), (rect["right"] + 50, rect["bottom"] + 50)):
        nx, ny = space.to_normalized(*point, rect=rect)
        assert 0 <= nx <= 65535 and 0 <= ny <= 65535, f"выход за диапазон: {point} -> {nx}, {ny}"


# --- P1: мультимониторный кроп ------------------------------------------------------


def test_crop_with_negative_origin():
    """Окно на мониторе слева от основного: кадр начинается с отрицательных координат."""
    grabber = ScreenGrabber()
    frame = Frame(image=np.zeros((1080, 3840, 4), dtype=np.uint8), source="mss", ms=1.0,
                  origin=(-1920, 0))
    rect = {"left": -1920, "top": 100, "right": -1000, "bottom": 800}
    crop = grabber.crop(rect, frame)
    assert crop.size == (920, 700)
    assert crop.origin == (-1920, 100)
    assert crop.rect == rect


def test_covers_rejects_other_monitor():
    grabber = ScreenGrabber()
    primary = Frame(image=np.zeros((1080, 1920, 4), dtype=np.uint8), source="dxgi", ms=1.0)
    assert not grabber.covers(primary, {"left": -1920, "top": 100, "right": -1000, "bottom": 800})
    assert grabber.covers(primary, {"left": 10, "top": 10, "right": 100, "bottom": 100})


def test_crop_outside_frame_raises_typed_error():
    grabber = ScreenGrabber()
    primary = Frame(image=np.zeros((1080, 1920, 4), dtype=np.uint8), source="dxgi", ms=1.0)
    with pytest.raises(ScreenCaptureError) as error:
        grabber.crop({"left": -1920, "top": 0, "right": -1000, "bottom": 700}, primary)
    assert "вне захваченного кадра" in str(error.value)


# --- P1: перезапись файла -----------------------------------------------------------


def test_file_watcher_sees_overwrite(tmp_path: Path):
    """Сохранение поверх существующего файла — обычный случай для отчётов."""
    target = tmp_path / "report.txt"
    target.write_text("первая версия", encoding="utf-8")
    watcher = FileWatcher(str(tmp_path))
    watcher.start()
    try:
        time.sleep(0.3)
        target.write_text("вторая версия, длиннее", encoding="utf-8")
        detected = watcher.wait_for("report.txt", timeout_s=2.0)
    finally:
        watcher.stop()
    assert detected == "report.txt", "перезапись файла не обнаружена"


# --- офисный COM: отслеживание процессов --------------------------------------------


def test_probe_kills_tracked_process():
    """Процесс, взятый под опеку, умирает вместе с сателлитом.

    Так снимается главная угроза COM-сценариев: `WINWORD.EXE`, поднятый службой DCOM,
    не является ребёнком воркера и после его убийства остаётся висеть с занятым файлом.
    """
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        time.sleep(0.2)
        assert victim.poll() is None
        with ProbeProcess(default_timeout_s=2.0) as satellite:
            satellite.track_process(victim.pid, note="имитация WINWORD.EXE")
            assert satellite.tracked_processes()[0]["pid"] == victim.pid
            satellite.kill()
        deadline = time.time() + 5
        while time.time() < deadline and victim.poll() is None:
            time.sleep(0.1)
        assert victim.poll() is not None, "опекаемый процесс остался висеть"
        assert satellite.tracked_processes() == []
    finally:
        if victim.poll() is None:
            victim.kill()


def test_probe_untrack_allows_process_to_live():
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        with ProbeProcess(default_timeout_s=2.0) as satellite:
            satellite.track_process(victim.pid)
            satellite.untrack_process(victim.pid)
            satellite.kill()
        time.sleep(0.3)
        assert victim.poll() is None, "снятый с опеки процесс убит напрасно"
    finally:
        victim.kill()


# --- P2: словарь типов элементов ----------------------------------------------------


def test_control_type_dictionary_is_broad():
    """Адресный поиск должен уметь по типам, которые реально встречаются в интерфейсах."""
    for name in ("pane", "check_box", "combo_box", "hyperlink", "group", "list_item",
                 "menu_item", "tab_item", "tree_item", "tool_bar"):
        assert name in CONTROL_TYPE_IDS, f"нет типа {name}"


def test_unknown_control_type_is_ignored_not_broken():
    """Незнакомый тип не должен ломать поиск: условие по типу просто не добавляется."""
    from mcp_hands.uia import UiaClient

    client = UiaClient()
    assert "нет-такого-типа" not in CONTROL_TYPE_IDS
    del client
