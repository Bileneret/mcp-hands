"""Регрессионные тесты по отчёту `reports/audit_stage1_v2_20261017.md`.

Проверяются четыре требования аудита: кроп максимизированных окон, ввод на чужом
рабочем столе, запрет слепого клика с абсолютными координатами нажатия и опека
процессов Office, созданных внутри сателлита.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from mcp_hands.geometry import CoordinateSpace
from mcp_hands.input import MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_LEFTDOWN, Actuator
from mcp_hands.probe import ProbeProcess
from mcp_hands.screen import Frame, ScreenCaptureError, ScreenGrabber

VIRTUAL_SCREEN = {"left": 0, "top": 0, "right": 1920, "bottom": 1080}


class Capture:
    """Перехват событий ввода: проверяем формирование, а не доставку."""

    def __init__(self, actuator: Actuator) -> None:
        self.actuator = actuator
        self.events: list[dict] = []
        self._original = actuator.user32.SendInput

    def __enter__(self) -> "Capture":
        def fake_send(count, pointer, _size):
            for index in range(count):
                event = pointer[index]
                self.events.append({
                    "kind": "mouse" if event.type == 0 else "key",
                    "flags": event.mi.dwFlags,
                    "x": event.mi.dx, "y": event.mi.dy,
                })
            return count

        self.actuator.user32.SendInput = fake_send
        return self

    def __exit__(self, *_exc) -> None:
        self.actuator.user32.SendInput = self._original


# --- P0: максимизированные окна -----------------------------------------------------


def test_covers_is_strict_but_intersects_is_not():
    """Строгое включение осталось для диагностики, кроп решает по пересечению.

    На стенде у развёрнутого окна за пределы экрана выходит `GetWindowRect`
    (невидимые поля изменения размера), а DWM-границы лежат в экране. Требование
    полного включения запрещало бы снимать любое такое окно.
    """
    grabber = ScreenGrabber()
    frame = Frame(image=np.zeros((1080, 1920, 4), dtype=np.uint8), source="mss", ms=1.0)
    oversized = {"left": -9, "top": -9, "right": 1929, "bottom": 1029}
    assert not grabber.covers(frame, oversized), "рамка выходит за кадр — covers обязан это видеть"
    assert grabber.intersects(frame, oversized), "видимая часть есть — кроп обязан работать"


def test_crop_window_handles_maximized_window(target, require_capture):
    """Развёрнутое окно снимается, а не отбрасывается проверкой включения."""
    import ctypes
    import ctypes.wintypes as wintypes

    space = CoordinateSpace()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    grabber = ScreenGrabber()

    user32.ShowWindow(wintypes.HWND(target.hwnd), 3)  # SW_MAXIMIZE
    time.sleep(0.8)
    try:
        frame = grabber.grab()
        crop = grabber.crop_window(space, target.hwnd, frame)
        raw = space.window_rect_raw(target.hwnd)      # с невидимыми полями
        frame_rect = space.window_frame(target.hwnd)  # видимая рамка
        assert crop.size[0] > 200 and crop.size[1] > 200
        assert grabber.intersects(frame, frame_rect), \
            f"видимая рамка {frame_rect} обязана пересекаться с кадром"
        if not grabber.covers(frame, raw):
            assert crop.suspicious or grabber.covers(frame, frame_rect), \
                "частичный кроп обязан быть помечен"
    finally:
        user32.ShowWindow(wintypes.HWND(target.hwnd), 9)  # SW_RESTORE


def test_crop_window_still_rejects_invisible_window():
    """Окно целиком за пределами кадра по-прежнему даёт типизированный отказ."""
    grabber = ScreenGrabber()
    frame = Frame(image=np.zeros((1080, 1920, 4), dtype=np.uint8), source="dxgi", ms=1.0)
    assert not grabber.intersects(frame, {"left": 3000, "top": 0, "right": 4000, "bottom": 500})


# --- P1: слепой клик запрещён -------------------------------------------------------


def test_click_blocks_when_cursor_position_unknown():
    """Позицию курсора подтвердить нельзя — нажатие не отправляется.

    Иначе клик ушёл бы в текущую позицию курсора, где может оказаться что угодно:
    кнопка закрытия чужого окна, меню, кнопка отправки.
    """
    actuator = Actuator()
    actuator.cursor = lambda: (0, 0)
    with Capture(actuator) as capture:
        result = actuator.click(600, 400, verify=True, settle_s=0)
    presses = [event for event in capture.events
               if event["kind"] == "mouse" and event["flags"] & MOUSEEVENTF_LEFTDOWN]
    assert not result["ok"] and not presses, f"слепой клик не заблокирован: {result}"
    assert result["cursor_check"] == "недоступен"
    assert "слепой клик заблокирован" in result["reason"]


def test_click_unverified_requires_explicit_flag():
    """Осознанный обход блокировки возможен только явным аргументом."""
    actuator = Actuator()
    actuator.cursor = lambda: (0, 0)
    with Capture(actuator) as capture:
        result = actuator.click(600, 400, verify=True, settle_s=0, allow_unverified=True)
    presses = [event for event in capture.events
               if event["kind"] == "mouse" and event["flags"] & MOUSEEVENTF_LEFTDOWN]
    assert result["ok"] and len(presses) == 1
    assert result["cursor_check"] == "недоступен"


def test_click_sends_absolute_coordinates():
    """Нажатие несёт координаты цели: промах перемещения не уводит клик в чужое место."""
    actuator = Actuator()
    actuator.cursor = lambda: (600, 400)
    with Capture(actuator) as capture:
        actuator.click(600, 400, verify=True, settle_s=0)
    presses = [event for event in capture.events
               if event["kind"] == "mouse" and event["flags"] & MOUSEEVENTF_LEFTDOWN]
    assert presses, "нажатие не отправлено"
    expected = actuator.space.to_normalized(600, 400)
    assert (presses[0]["x"], presses[0]["y"]) == expected, \
        "нажатие ушло без абсолютных координат цели"
    assert presses[0]["flags"] & MOUSEEVENTF_ABSOLUTE


def test_sendinput_failure_reports_winerror(monkeypatch):
    """Отказ SendInput обязан вернуть причину, а не пустой результат."""
    actuator = Actuator()
    actuator.cursor = lambda: (600, 400)

    def failing_send(_count, _pointer, _size):
        import ctypes

        ctypes.set_last_error(5)  # ERROR_ACCESS_DENIED
        return 0

    monkeypatch.setattr(actuator.user32, "SendInput", failing_send)
    result = actuator.click(600, 400, verify=True, settle_s=0)
    assert not result["ok"]
    assert "WinError 5" in result["reason"] and "UIPI" in result["reason"]


def test_actuator_reports_input_desktop():
    """Диагностика рабочего стола ввода: результат и причина отказа, если он случился.

    В тестовом процессе привязка может не удаться (SetThreadDesktop возвращает 170,
    если у потока уже есть окна — их создаёт comtypes для маршалинга COM). Это нормально
    и должно быть видно как причина, а не как падение. Рабочее место привязки — сателлит,
    где она выполняется до создания окон (проверяется тестом `test_worker_ping_reports_desktop`).
    """
    actuator = Actuator()
    info = actuator.attach_to_input_desktop()
    assert "attached" in info
    if not info["attached"]:
        assert info["reason"], "отказ обязан сопровождаться причиной"


def test_worker_ping_reports_desktop():
    """Сателлит обязан сообщать состояние рабочего стола: без него ввод не доходит."""
    with ProbeProcess(default_timeout_s=3.0) as satellite:
        info = satellite.ping()
    assert "desktop" in info
    assert info["desktop"].get("name"), f"имя рабочего стола не получено: {info}"


# --- P1: опека процессов Office, созданных внутри сателлита --------------------------


def test_satellite_registers_process_in_shared_file():
    """Воркер записывает pid в общий файл: супервизор подберёт его после убийства.

    Так закрывается архитектурный разрыв: если воркер зависнет внутри COM-вызова,
    ответа родителю не будет, но запись в файле останется.
    """
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        time.sleep(0.2)
        with ProbeProcess(default_timeout_s=3.0) as satellite:
            answer = satellite.call("register_process", pid=victim.pid,
                                    image="python.exe", note="имитация WINWORD.EXE")
            assert answer["ok"], answer
            assert satellite.pid_file.exists(), "файл-реестр не создан"
            entries = json.loads(satellite.pid_file.read_text(encoding="utf-8"))
            assert entries[0]["pid"] == victim.pid
            # Родитель ничего не знает об этом процессе — узнаёт из файла.
            assert satellite.tracked_processes() == []
            satellite.kill()
        deadline = time.time() + 5
        while time.time() < deadline and victim.poll() is None:
            time.sleep(0.1)
        assert victim.poll() is not None, "процесс из реестра не был убит"
    finally:
        if victim.poll() is None:
            victim.kill()


def test_kill_tracked_verifies_image_name():
    """Проверка образа перед убийством: при несовпадении процесс не трогаем."""
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        time.sleep(0.2)
        with ProbeProcess(default_timeout_s=2.0) as satellite:
            satellite.track_process(victim.pid, note="тест", image="WINWORD.EXE")
            report = satellite.kill_tracked()
        assert report and not report[0]["killed"]
        assert "PID переиспользован" in report[0]["reason"]
        time.sleep(0.3)
        assert victim.poll() is None, "чужой процесс убит по переиспользованному номеру"
    finally:
        victim.kill()


def test_kill_tracked_reports_missing_process():
    """Если процесс уже завершился, это отчёт, а не ошибка."""
    victim = subprocess.Popen([sys.executable, "-c", "pass"])
    victim.wait(timeout=10)
    with ProbeProcess(default_timeout_s=2.0) as satellite:
        satellite.track_process(victim.pid, image="python.exe")
        report = satellite.kill_tracked()
    assert report[0]["killed"] is False
    assert "уже нет" in report[0]["reason"]


# --- P2: живая проверка ввода (аудит: моки маскируют отказы среды) ---------------------


def test_native_input_reaches_window(target):
    """Живая проверка: настоящий SendInput доходит до кнопки мишени.

    Моки проверяют формирование структур, но не доставку. Здесь ввод отправляется
    по-настоящему, а мишень сама подтверждает попадание в своём журнале.
    """
    from mcp_hands.uia import UiaClient

    actuator = Actuator()
    if not actuator.attach_to_input_desktop().get("attached"):
        pytest.skip("ввод недоступен: поток не привязался к рабочему столу ввода")

    client = UiaClient()
    node = client.find(client.content_root(target.hwnd)[0], name="beta", control_type="button")
    assert node is not None
    result = actuator.click(*node["center"], verify=True)
    if not result["ok"] and result.get("cursor_check") == "недоступен":
        pytest.skip(f"позиция курсора недоступна в этой сессии: {result.get('reason')}")
    assert result["ok"], f"живой клик не выполнен: {result}"
    deadline = time.time() + 2
    while time.time() < deadline and not target.clicks():
        time.sleep(0.05)
    assert any("beta" in line for line in target.clicks()), \
        f"мишень не подтвердила попадание: {target.events()}"
