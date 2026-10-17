"""Страж ввода: клавиши уходят только в проверенно активное окно.

Эти проверки не отправляют настоящий ввод: на отказе `SendInput` не вызывается вовсе
(это и есть предмет проверки), а на разрешении вызов перехватывается. Отдельный живой
тест работает только с нашей мишенью.
"""

from __future__ import annotations

import ctypes
import time

import pytest

from mcp_hands.geometry import CoordinateSpace
from mcp_hands.input import INPUT_KEYBOARD, VK_CONTROL, VK_S, Actuator
from mcp_hands.uia import find_window


class Spy:
    """Перехватывает SendInput: считает события, не отправляя их в систему."""

    def __init__(self, actuator: Actuator) -> None:
        self.actuator = actuator
        self.events = 0
        self._original = actuator.user32.SendInput

    def __enter__(self) -> "Spy":
        def fake(count, _pointer, _size):
            self.events += count
            return count

        self.actuator.user32.SendInput = fake
        return self

    def __exit__(self, *_exc) -> None:
        self.actuator.user32.SendInput = self._original


def test_keyboard_without_target_is_refused():
    """Без указания ожидаемого окна ввод запрещён: именно так нажатие уходило в чужое."""
    actuator = Actuator(CoordinateSpace())
    with Spy(actuator) as spy:
        result = actuator.hotkey(VK_CONTROL, VK_S)
        typed = actuator.type_text("текст")
    assert not result["ok"] and not typed["ok"]
    assert "не указано ожидаемое окно" in result["reason"]
    assert spy.events == 0, "нажатия всё же ушли в систему"


def test_keyboard_with_wrong_target_is_refused(target):
    """Активно чужое окно — нажатие отменяется с указанием, что именно было активно."""
    actuator = Actuator(CoordinateSpace())
    actuator.activate(target.hwnd)
    time.sleep(0.3)
    wrong = target.hwnd + 4242  # заведомо не то окно
    with Spy(actuator) as spy:
        result = actuator.hotkey(VK_CONTROL, VK_S, expect_hwnd=wrong)
        typed = actuator.type_text("текст", expect_hwnd=wrong)
    assert not result["ok"] and not typed["ok"]
    assert "активно не наше окно" in result["reason"]
    assert spy.events == 0


def test_keyboard_with_correct_target_is_allowed(target, require_input_desktop):
    """Наше окно активно — ввод выполняется (события формируются)."""
    actuator = Actuator(CoordinateSpace())
    activation = actuator.activate(target.hwnd)
    time.sleep(0.3)
    assert activation.get("ok"), activation
    assert actuator.foreground() == target.hwnd
    with Spy(actuator) as spy:
        result = actuator.hotkey(VK_CONTROL, VK_S, expect_hwnd=target.hwnd)
    assert result["ok"], result
    # Ctrl и S: нажатие и отпускание каждой клавиши — четыре события.
    assert spy.events == 4, f"ожидались четыре события, получено {spy.events}"


def test_explicit_opt_out_still_works():
    """Осознанный обход возможен только явным аргументом — и он виден в коде."""
    actuator = Actuator(CoordinateSpace())
    with Spy(actuator) as spy:
        result = actuator.hotkey(VK_CONTROL, VK_S, allow_any_window=True)
    assert result["ok"] and spy.events == 4


def test_guard_reports_what_was_foreground(target, require_input_desktop):
    """В отказе видно, какое окно было активным: это нужно для разбора инцидентов."""
    actuator = Actuator(CoordinateSpace())
    actuator.activate(target.hwnd)
    time.sleep(0.3)
    info = actuator.foreground_info()
    assert info["hwnd"] == target.hwnd
    assert info["title"], "заголовок активного окна не получен"


def test_nothing_happens_when_target_window_is_gone():
    """Страж не падает, если окна-цели больше нет: просто отказывает."""
    actuator = Actuator(CoordinateSpace())
    with Spy(actuator) as spy:
        result = actuator.hotkey(VK_CONTROL, VK_S, expect_hwnd=0x7FFFFFFF)
    assert not result["ok"]
    assert spy.events == 0
