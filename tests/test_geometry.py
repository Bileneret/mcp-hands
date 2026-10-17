"""Проверки координатного пространства: арифметика перевода и границы окна.

Здесь нет окон: проверяется то, что обязано быть верным арифметически, включая
конфигурацию с монитором слева от основного (отрицательные координаты виртуального
рабочего стола) — на ней ломались первые версии проверки позиции курсора.
"""

from __future__ import annotations

import ctypes

from mcp_hands.geometry import CoordinateSpace, Monitor, process_awareness, rect_center


def test_process_is_dpi_aware():
    """Без Per-Monitor V2 координаты виртуализуются и клики промахиваются на 25%."""
    awareness = process_awareness()
    assert awareness["code"] == 2, f"ожидался per-monitor режим, получено {awareness}"
    assert awareness["name"] == "PER_MONITOR_DPI_AWARE"


def test_normalized_round_trip():
    """Перевод в 0..65535 и обратно не должен терять больше пикселя."""
    space = CoordinateSpace()
    for point in ((0, 0), (10, 10), (960, 540), (1919, 1079)):
        normalized = space.to_normalized(*point)
        restored = space.from_normalized(*normalized)
        assert abs(restored[0] - point[0]) <= 1, f"x: {point} -> {normalized} -> {restored}"
        assert abs(restored[1] - point[1]) <= 1, f"y: {point} -> {normalized} -> {restored}"


def test_normalized_bounds_are_extremes():
    """Границы диапазона обязаны соответствовать краям виртуального экрана."""
    space = CoordinateSpace()
    area = space.virtual_rect
    assert space.to_normalized(area["left"], area["top"]) == (0, 0)
    right, bottom = space.to_normalized(area["right"] - 1, area["bottom"] - 1)
    assert (right, bottom) == (65535, 65535)


def test_virtual_rect_covers_all_monitors():
    """Виртуальный прямоугольник обязан накрывать каждый монитор."""
    space = CoordinateSpace()
    area = space.virtual_rect
    assert space.monitors, "мониторы не найдены"
    for monitor in space.monitors:
        assert area["left"] <= monitor.rect["left"] and area["top"] <= monitor.rect["top"]
        assert area["right"] >= monitor.rect["right"] and area["bottom"] >= monitor.rect["bottom"]


def test_normalized_with_negative_origin():
    """Монитор слева от основного: смещение виртуального экрана обязано учитываться.

    Конфигурация синтезируется вручную: на стенде один монитор, поэтому проверяется
    арифметика, а не живой клик.
    """
    space = CoordinateSpace()
    synthetic = {"left": -1920, "top": -200, "right": 1920, "bottom": 880}
    normalized = space.to_normalized(-1920, -200, rect=synthetic)
    assert normalized == (0, 0)
    restored = space.from_normalized(*normalized, rect=synthetic)
    assert restored == (-1920, -200)
    center = space.from_normalized(32767, 32767, rect=synthetic)
    assert -1920 < center[0] < 1920 and -200 < center[1] < 880


def test_window_frame_excludes_shadow():
    """Границы окна берутся из DWM: GetWindowRect шире видимой рамки на тень."""
    space = CoordinateSpace()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return
    raw = ctypes.wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(raw))
    frame = space.window_frame(int(hwnd))
    assert frame["right"] - frame["left"] <= raw.right - raw.left
    assert frame["bottom"] - frame["top"] <= raw.bottom - raw.top
    assert frame["right"] > frame["left"] and frame["bottom"] > frame["top"]


def test_monitor_scale_is_plausible():
    """DPI монитора осмыслен: 96 (100%) и выше."""
    for monitor in CoordinateSpace().monitors:
        assert monitor.dpi >= 96
        assert 1.0 <= monitor.scale <= 4.0
        assert isinstance(monitor, Monitor)


def test_rect_center():
    assert rect_center({"left": 10, "top": 20, "right": 30, "bottom": 60}) == (20, 40)
