"""Регрессионные тесты по отчёту `reports/audit_stage1_v4_20261017.md`.

Проверяется привязка мишени к рабочему столу ввода и отказ с объяснением, когда окно
цели живёт на другом рабочем столе: ввод физически уходит только на стол вызывающего
потока, и молчаливый клик в пустоту недопустим.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes

import pytest

from mcp_hands.geometry import CoordinateSpace
from mcp_hands.input import Actuator
from mcp_hands.testing.fixture_window import attach_to_input_desktop


def test_fixture_attaches_to_input_desktop():
    """Мишень обязана уметь вставать на рабочий стол ввода.

    Иначе в песочнице или служебном сеансе её окно окажется на параллельном столе,
    и щелчки, отправленные на стол ввода, до него не дойдут — тесты ввода будут
    падать по вине среды.
    """
    info = attach_to_input_desktop()
    assert "attached" in info
    assert info["name"], "имя рабочего стола ввода не получено"


def test_target_window_is_on_input_desktop(target, aligned_desktop):
    """Окно мишени лежит на том же рабочем столе, что и процесс тестов.

    Если выравнивание не удалось (в сессии с чужим рабочим столом), проверка
    пропускается с причиной: расхождение столов — отказ среды, а не ошибка кода.
    """
    actuator = Actuator(CoordinateSpace())
    ours = actuator.own_desktop()
    theirs = actuator.window_desktop(target.hwnd)
    assert ours and theirs
    assert target.desktop == theirs, \
        f"мишень сообщила {target.desktop!r}, окно фактически на {theirs!r}"
    if ours != theirs:
        pytest.skip(f"процесс тестов на {ours!r}, окно мишени на {theirs!r}; "
                    f"выравнивание: {aligned_desktop.get('reason') or aligned_desktop.get('how')}")


def test_click_refuses_window_on_other_desktop(target, monkeypatch):
    """Чужой рабочий стол — явный отказ с обоими именами, а не клик в пустоту."""
    actuator = Actuator(CoordinateSpace())
    monkeypatch.setattr(Actuator, "window_desktop", lambda self, hwnd: "exebox-1234")
    node = {"name": "beta", "control_type": "button", "rect": [10, 10, 60, 40],
            "center": [35, 25], "process_id": 0}
    result = actuator.click_node(node, window_hwnd=target.hwnd)
    assert not result["ok"]
    assert "клик туда не дойдёт" in result["reason"]
    assert result["desktop"] == {"ours": actuator.own_desktop(), "target": "exebox-1234"}


def test_unreadable_desktop_does_not_block_click(target, monkeypatch):
    """Если имя стола прочитать не удалось, клик не блокируется.

    Отказ должен быть следствием установленного расхождения, а не отсутствия данных.
    """
    actuator = Actuator(CoordinateSpace())
    monkeypatch.setattr(Actuator, "window_desktop", lambda self, hwnd: "<ошибка 5>")
    monkeypatch.setattr(Actuator, "cursor", lambda self: (600, 400))
    node = {"name": "beta", "control_type": "button", "rect": [560, 460, 810, 580],
            "center": [600, 400], "process_id": 0}
    result = actuator.click_node(node, window_hwnd=target.hwnd)
    assert result["desktop"]["target"] == "<ошибка 5>"
    assert result.get("cursor_check") in ("проверен", "недоступен"), result
