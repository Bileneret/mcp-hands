"""Проверки событийного слоя: поток с очередью сообщений и ожидание файлов.

Ключевой факт, который здесь закрепляется: колбэк внепроцессного хука вызывается
в потоке, поставившем хук, и только внутри обработки сообщений. Если поток не крутит
очередь, событий не приходит вовсе — проверено отдельным замером, поэтому ожидание
в сценариях строится только на этом классе.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mcp_hands.events import EVENT_OBJECT_SHOW, EVENT_SYSTEM_FOREGROUND, EventPump, FileWatcher


@pytest.fixture
def pump():
    with EventPump(events=(EVENT_SYSTEM_FOREGROUND, EVENT_OBJECT_SHOW)) as instance:
        assert instance.start(), "поток событий не поднялся"
        yield instance


def test_pump_starts_with_hooks(pump):
    health = pump.health()
    assert health["running"]
    assert health["hooks"] >= 2, f"хуки не встали: {health}"
    assert not health["errors"], health["errors"]
    assert health["thread_id"] > 0, "поток обязан сообщить свой идентификатор для остановки"


def test_window_event_is_delivered(pump, target, require_input_desktop):
    """Событие о появлении окна приходит; задержка доставки — доли миллисекунды."""
    event = pump.wait_for_window(title_contains="pytest target", timeout_s=2.0)
    assert event is not None, "событие о появлении окна не получено"
    assert event.hwnd == target.hwnd
    assert event.latency_ms is not None
    assert event.latency_ms < 50, f"подозрительно большая задержка доставки: {event.latency_ms} мс"
    described = event.describe()
    assert described["kind"] in ("show", "foreground", "create")


def test_events_are_queued_and_drained(pump, target, require_input_desktop):
    """События не теряются: они ждут в очереди, пока их не разберут."""
    time.sleep(0.3)
    events = pump.drain()
    assert events, "очередь пуста, хотя окно создавалось"
    assert all(event.hwnd > 0 for event in events)


def test_stop_is_clean(pump):
    pump.stop()
    assert not pump.health()["running"]


def test_file_watcher_sees_new_file(tmp_path: Path):
    """Появление файла ловится уведомлением, а не опросом каталога."""
    watcher = FileWatcher(str(tmp_path))
    assert watcher.start(), "каталог не открылся для наблюдения"
    try:
        started = time.perf_counter()
        (tmp_path / "report.txt").write_text("готово", encoding="utf-8")
        name = watcher.wait_for("report.txt", timeout_s=3.0)
        elapsed_ms = (time.perf_counter() - started) * 1000
    finally:
        watcher.stop()
    assert name == "report.txt", f"уведомление не пришло: {name}"
    assert elapsed_ms < 500, f"уведомление о файле пришло за {elapsed_ms:.0f} мс"


def test_file_watcher_timeout_is_silent(tmp_path: Path):
    """Ожидание несуществующего файла завершается по таймауту, а не висит."""
    watcher = FileWatcher(str(tmp_path))
    watcher.start()
    try:
        started = time.perf_counter()
        result = watcher.wait_for("нет-такого.txt", timeout_s=0.4)
    finally:
        watcher.stop()
    assert result is None
    assert time.perf_counter() - started < 2.0
