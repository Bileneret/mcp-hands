"""Проверки процесса-сателлита: адресный поиск, клик и снятие зависшего вызова.

Главная ценность здесь — тест на окне, которое перестало отвечать: без сателлита
вызов UIA не возвращается вообще (замер: не вернулся и за 45 с), и агент зависает.
Проверяется не «стало быстрее», а «агент получил явный отказ и продолжил работу».
"""

from __future__ import annotations

import time

import pytest

from mcp_hands.probe import ProbeError, ProbeProcess, ProbeTimeout


@pytest.fixture(scope="module")
def probe():
    with ProbeProcess(default_timeout_s=1.0) as satellite:
        yield satellite


def test_ping_and_process_is_persistent(probe):
    """Сателлит долгоживущий: два вызова обслуживает один и тот же процесс.

    Замерено 300–800 мс на запуск Python и импорт comtypes — на каждый шаг это неприемлемо,
    поэтому процесс обязан сохраняться между вызовами.
    """
    first = probe.ping()
    second = probe.ping()
    assert first["pid"] == second["pid"], "сателлит перезапускается на каждый вызов"
    assert first["awareness"] == "PER_MONITOR_DPI_AWARE"
    assert probe.alive


def test_find_element_by_name(probe, target):
    """Адресный поиск кнопки по имени — за один переход к чужому процессу."""
    node = probe.element(target.hwnd, name="alpha", control_type="button")
    assert node is not None, "кнопка alpha не найдена через UIA"
    assert node["name"] == "alpha"
    assert node["control_type"] == "UIA_ButtonControlTypeId"
    assert node["rect"][2] > node["rect"][0], "некорректный прямоугольник элемента"


def test_find_element_missing_returns_none(probe, target):
    assert probe.element(target.hwnd, name="нет-такой-кнопки") is None


def test_walk_returns_levels(probe, target):
    """Обход даёт уровни и узлы; после прыжка к контенту или отката — непустой первый уровень."""
    result = probe.walk(target.hwnd, depth=3)
    assert result["levels"], "уровни не собраны"
    assert result["levels"][0]["elements"] > 0, "первый уровень пуст — прыжок к контенту не сработал"
    assert result["nodes"], "узлы не собраны"


def test_click_reaches_element(probe, target):
    """Клик через сателлит доходит до кнопки: мишень сама подтверждает попадание."""
    result = probe.click(target.hwnd, name="beta", control_type="button")
    assert result.get("ok"), f"клик не выполнен: {result}"
    deadline = time.time() + 2
    while time.time() < deadline and not target.clicks():
        time.sleep(0.05)
    assert any("beta" in line for line in target.clicks()), \
        f"мишень не зафиксировала нажатие: {target.events()}"


def test_rights_check_reports_integrity(probe, target):
    """Проверка целостности: ввод в окно с более высокими правами не дойдёт (UIPI)."""
    rights = probe.call("rights", pid=target.pid)
    assert rights["target"]["level"] is not None
    assert rights["blocked_by_uipi"] is False  # мишень запущена с теми же правами


def test_hung_window_gives_explicit_refusal(hang_target):
    """Зависшее окно: агент получает явный отказ в бюджете, а не зависает.

    Наблюдаемое поведение: `ConnectionTimeout` UIA превращает молчание провайдера
    в ошибку `0x80040201` (событие не доставлено ни одному подписчику) — это и есть
    защита в действии. Без неё вызов не возвращался и за 45 с (замер этапа 0).
    """
    with ProbeProcess(default_timeout_s=0.3) as satellite:
        first_pid = satellite.ping()["pid"]
        started = time.perf_counter()
        with pytest.raises((ProbeTimeout, ProbeError)):
            satellite.walk(hang_target.hwnd, depth=2, timeout_s=0.3)
        elapsed = time.perf_counter() - started
        assert elapsed < 2.0, f"отказ по зависшему окну занял {elapsed:.2f} с"
        assert satellite.stats.timeouts + satellite.stats.errors >= 1

        recovered = satellite.ping()
        assert recovered["pid"] > 0, "сателлит обязан продолжить работу"
        if satellite.stats.timeouts:
            assert recovered["pid"] != first_pid, "после убийства поднимается новый процесс"


def test_timeout_kills_and_restarts_satellite():
    """Бюджет меньше операции: процесс убивается, следующий вызов обслуживает новый.

    Это единственный способ разблокировать вызов UIA, зависший на чужом провайдере:
    изнутри процесса такой вызов не прервать.
    """
    with ProbeProcess(default_timeout_s=0.2) as satellite:
        first_pid = satellite.ping()["pid"]
        with pytest.raises(ProbeTimeout):
            satellite.call("sleep", seconds=1.5, timeout_s=0.2)
        assert not satellite.alive, "по таймауту сателлит обязан быть убит"
        assert satellite.stats.timeouts >= 1
        assert satellite.stats.restarts >= 1
        restarted = satellite.ping()
        assert restarted["pid"] != first_pid, "сателлит не перезапустился"


def test_stats_are_recorded(probe):
    stats = probe.stats.describe()
    assert stats["calls"] > 0
    assert stats["avg_ms"] is not None
    assert stats["history"], "история вызовов пуста — отказы нечем объяснить"
