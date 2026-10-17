"""Проверка утверждений аудита v3: таймаут click, цена обхода окон, коллизия файла-реестра.

Запускается файлом: сателлит использует `multiprocessing`.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from conftest import Target  # noqa: E402
from mcp_hands.probe import ProbeProcess  # noqa: E402
from mcp_hands.probe_worker import Worker  # noqa: E402


def main() -> int:
    target = Target("mcp_hands.testing.fixture_window", "MCP-Hands audit v3 target")
    try:
        worker = Worker()
        print("1) время операций внутри сателлита (в этом процессе, без IPC):")
        for label, op, kwargs in (
            ("windows (обход всех окон)", "windows", {}),
            ("element (поиск кнопки)", "element", {"hwnd": target.hwnd, "name": "beta",
                                                   "control_type": "button"}),
            ("click (поиск + активация + клик)", "click", {"hwnd": target.hwnd, "name": "beta",
                                                           "control_type": "button"}),
        ):
            started = time.perf_counter()
            worker.handle(op, **kwargs)
            print(f"   {label:<34} {(time.perf_counter() - started) * 1000:8.1f} мс")

        print("\n2) бюджет 1.0 с против фактического времени click:")
        with ProbeProcess(default_timeout_s=1.0) as satellite:
            try:
                started = time.perf_counter()
                result = satellite.click(target.hwnd, name="beta", control_type="button")
                print(f"   уложился: {(time.perf_counter() - started) * 1000:.0f} мс, ok={result.get('ok')}")
            except Exception as exc:  # noqa: BLE001
                print(f"   ОТКАЗ через {(time.perf_counter() - started) * 1000:.0f} мс: "
                      f"{type(exc).__name__}: {exc}")
            print(f"   статистика: {satellite.stats.describe()}")

        print("\n3) имена файлов-реестров у двух экземпляров:")
        with ProbeProcess(default_timeout_s=2.0) as first, ProbeProcess(default_timeout_s=2.0) as second:
            print(f"   первый:  {first.pid_file.name}")
            print(f"   второй:  {second.pid_file.name}")
            print(f"   совпадают: {first.pid_file == second.pid_file}")

        print("\n4) process_id в узле UIA (доступен ли без обхода окон):")
        node = worker.handle("element", hwnd=target.hwnd, name="beta", control_type="button")
        print(f"   process_id в узле: {node.get('process_id')} | PID мишени: {target.pid}")
    finally:
        target.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
