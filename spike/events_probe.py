"""Событийный слой: SetWinEventHook и ReadDirectoryChangesW против поллинга.

Пауза «на глазок» стоит дорого в обе стороны: короткая жжёт CPU, длинная добавляет задержку
к каждому шагу агента. Замер показывает, сколько именно выигрывает событийный путь:

  * появление окна — хук `EVENT_OBJECT_SHOW` против цикла с паузой 50 мс;
  * появление файла — `ReadDirectoryChangesW` против того же цикла.

Задержка события берётся из поля `time` самого события (миллисекунды с запуска системы),
а не от момента, когда мы решили начать ждать, — иначе в замер попадает время работы
тестового окна, а не доставка уведомления.

    python spike/events_probe.py
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wintypes
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SPIKE_DIR = Path(__file__).resolve().parent
CREATE_NO_WINDOW = getattr(subprocess_flag := __import__("subprocess"), "CREATE_NO_WINDOW", 0)

EVENT_OBJECT_SHOW = 0x8002
EVENT_OBJECT_CREATE = 0x8000
EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
PM_REMOVE = 0x0001
FILE_NOTIFY_CHANGE_FILE_NAME = 0x00000001
FILE_ACTION_ADDED = 0x00000001
INFINITE = 0xFFFFFFFF


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.POINTER(ctypes.c_ulong)), ("InternalHigh", ctypes.POINTER(ctypes.c_ulong)),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]


WIN_EVENT_PROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                                    ctypes.c_long, ctypes.c_long, wintypes.DWORD, wintypes.DWORD)


class EventWatch:
    """Подписка на появление окон. Колбэк обязан жить, пока живёт хук."""

    def __init__(self, title_filter: str = "") -> None:
        import threading

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE,
                                           WIN_EVENT_PROC, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
        user32.SetWinEventHook.restype = wintypes.HANDLE
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, ctypes.c_uint,
                                        ctypes.c_uint, ctypes.c_uint]
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetTickCount64.argtypes = []
        kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        ctypes.WinDLL("user32", use_last_error=True).UnhookWinEvent.argtypes = [wintypes.HANDLE]

        self.user32 = user32
        self.kernel32 = kernel32
        self.title_filter = title_filter
        self.latencies: list[float] = []
        self.seen: list[dict] = []
        self.signal = threading.Event()
        self._callback = WIN_EVENT_PROC(self._on_event)
        self._hook = user32.SetWinEventHook(EVENT_OBJECT_SHOW, EVENT_OBJECT_SHOW, None,
                                            self._callback, 0, 0,
                                            WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)

    def _on_event(self, hook, event, hwnd, object_id, child_id, thread_id, event_time) -> None:
        if object_id != 0:  # OBJID_WINDOW: только сами окна, не подэлементы
            return
        buffer = ctypes.create_unicode_buffer(256)
        self.user32.GetWindowTextW(hwnd, buffer, 256)
        if self.title_filter and self.title_filter not in buffer.value:
            return
        # События внепроцессного хука приходят на поток ОС, а не в наш цикл сообщений,
        # поэтому наружу сигналим событием threading, а не через общий список.
        now = self.kernel32.GetTickCount64() & 0xFFFFFFFF
        latency = float((now - event_time) & 0xFFFFFFFF)
        self.latencies.append(latency)
        self.seen.append({"title": buffer.value, "hwnd": int(hwnd or 0), "latency_ms": latency})
        self.signal.set()

    def pump_until(self, seconds: float) -> float | None:
        """Ждёт первое подходящее событие и возвращает время ожидания в миллисекундах."""
        started = time.perf_counter()
        message = wintypes.MSG()
        while True:
            if self.signal.wait(0.002):
                return (time.perf_counter() - started) * 1000.0
            if time.perf_counter() - started > seconds:
                return None
            while self.user32.PeekMessageW(ctypes.byref(message), None, 0, 0, PM_REMOVE):
                self.user32.TranslateMessage(ctypes.byref(message))
                self.user32.DispatchMessageW(ctypes.byref(message))

    def close(self) -> None:
        if self._hook:
            self.user32.UnhookWinEvent(self._hook)
            self._hook = None


def wait_window_by_polling(title_filter: str, timeout: float, interval: float) -> float | None:
    """Поллинг: сколько ждать появления окна, если проверять раз в `interval` секунд."""
    import uia_common

    started = time.perf_counter()
    deadline = started + timeout
    while time.perf_counter() < deadline:
        if any(title_filter in window["title"] for window in uia_common.enum_windows()):
            return (time.perf_counter() - started) * 1000.0
        time.sleep(interval)
    return None


def file_watch_latency(directory: Path, name: str, timeout: float = 5.0,
                       poll_interval: float = 0.05) -> dict:
    """Появление файла: событие и поллинг стартуют ДО создания и меряются от одной точки."""
    import threading

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.ReadDirectoryChangesW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                               wintypes.BOOL, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                               ctypes.POINTER(OVERLAPPED), ctypes.c_void_p]
    kernel32.ReadDirectoryChangesW.restype = wintypes.BOOL

    handle = kernel32.CreateFileW(str(directory),
                                  0x0001,                                # FILE_LIST_DIRECTORY
                                  0x00000007,                            # share read/write/delete
                                  None, 3, 0x02000000, None)             # OPEN_EXISTING, BACKUP_SEMANTICS
    if handle == wintypes.HANDLE(-1).value:
        return {"error": f"CreateFileW: {ctypes.get_last_error()}"}

    target = directory / name
    stop = threading.Event()
    created_at: list[float] = []
    event_at: list[float] = []
    poll_at: list[float] = []
    buffer = ctypes.create_string_buffer(4096)
    returned = wintypes.DWORD(0)

    def events() -> None:
        while not stop.is_set():
            if kernel32.ReadDirectoryChangesW(handle, buffer, ctypes.sizeof(buffer), False,
                                              FILE_NOTIFY_CHANGE_FILE_NAME, ctypes.byref(returned),
                                              None, None) and returned.value and created_at:
                event_at.append(time.perf_counter())
                return

    def polling() -> None:
        while not stop.is_set():
            if created_at and target.exists():
                poll_at.append(time.perf_counter())
                return
            time.sleep(poll_interval)

    workers = [threading.Thread(target=events, daemon=True), threading.Thread(target=polling, daemon=True)]
    for worker in workers:
        worker.start()
    # Пауза намеренно НЕ кратна интервалу поллинга: при кратном значении проверка
    # попадает ровно в момент создания файла и «поллинг» выглядит мгновенным.
    time.sleep(0.137)
    created_at.append(time.perf_counter())
    target.write_text("probe", encoding="utf-8")

    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline and not (event_at and poll_at):
        time.sleep(0.01)
    stop.set()
    kernel32.CloseHandle(handle)

    return {
        "event_ms": round((event_at[0] - created_at[0]) * 1000.0, 3) if event_at else None,
        "polling_ms": round((poll_at[0] - created_at[0]) * 1000.0, 3) if poll_at else None,
        "poll_interval_ms": int(poll_interval * 1000),
    }


def kill_fixtures() -> None:
    """Убирает окна прошлых запусков: иначе поллинг «находит» их мгновенно."""
    import subprocess

    subprocess.run(["taskkill", "/F", "/IM", "python.exe", "/FI", "WINDOWTITLE eq *fixture*"],
                   capture_output=True, text=True)


def window_test(timeout: float, poll_interval: float, run_index: int) -> dict:
    import subprocess

    title_filter = f"fixture-{run_index}-{os.getpid()}"
    launch = [sys.executable, "-u", str(SPIKE_DIR / "fixture_window.py"),
              "--title", f"MCP-Hands {title_filter}",
              "--click-log", str(SPIKE_DIR / ".events_clicks.log")]

    watch = EventWatch(title_filter)
    try:
        started = time.perf_counter()
        fixture = subprocess.Popen(launch, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        event_wait = watch.pump_until(timeout)
        event_wait_ms = event_wait if event_wait is not None else (time.perf_counter() - started) * 1000.0
        fixture.kill()
        fixture.communicate()
    finally:
        watch.close()

    fixture = subprocess.Popen(launch, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        started = time.perf_counter()
        poll_ms = wait_window_by_polling(title_filter, timeout, poll_interval)
        if poll_ms is not None:
            poll_ms = (time.perf_counter() - started) * 1000.0
    finally:
        fixture.kill()
        fixture.communicate()

    return {
        "event_wait_ms": round(event_wait_ms, 1),
        "event_delivery_ms": round(statistics.median(watch.latencies), 3) if watch.latencies else None,
        "event_deliveries": len(watch.latencies),
        "poll_interval_ms": int(poll_interval * 1000),
        "poll_wait_ms": round(poll_ms, 1) if poll_ms else None,
        "note": "event_wait включает запуск процесса окна; event_delivery — чистая доставка хука",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="События против поллинга")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--poll-interval", type=float, default=0.05)
    parser.add_argument("--repeats", type=int, default=5, help="повторов для статистики доставки")
    parser.add_argument("--json", default=str(SPIKE_DIR / "results_events.json"))
    args = parser.parse_args()

    if not ctypes.windll.shcore.SetProcessDpiAwareness(2):
        pass

    report: dict = {}
    window_samples = []
    for index in range(max(1, args.repeats)):
        window_samples.append(window_test(args.timeout, args.poll_interval, index))
    report["window"] = {
        "event_wait_ms": round(statistics.median([s["event_wait_ms"] for s in window_samples]), 1),
        "event_wait_min_ms": round(min(s["event_wait_ms"] for s in window_samples), 1),
        "event_delivery_ms": round(statistics.median(
            [s["event_delivery_ms"] for s in window_samples if s["event_delivery_ms"] is not None]), 2),
        "poll_interval_ms": window_samples[0]["poll_interval_ms"],
        "poll_wait_ms": round(statistics.median(
            [s["poll_wait_ms"] for s in window_samples if s["poll_wait_ms"] is not None]), 1),
        "samples": window_samples,
    }

    with tempfile.TemporaryDirectory() as directory:
        directory_path = Path(directory)
        results = [file_watch_latency(directory_path, f"probe_{index}.txt", poll_interval=args.poll_interval)
                   for index in range(args.repeats)]
    report["file"] = {
        "event_ms": round(statistics.median([r["event_ms"] for r in results if r.get("event_ms")]), 3),
        "polling_ms": round(statistics.median([r["polling_ms"] for r in results if r.get("polling_ms")]), 3),
        "poll_interval_ms": int(args.poll_interval * 1000),
        "samples": results,
    }

    Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    window = report["window"]
    print(f"Окно: событие о появлении — доставка хука {window['event_delivery_ms']} мс "
          f"(медиана по {args.repeats} запускам), ожидание от старта процесса "
          f"{window['event_wait_ms']} мс (мин {window['event_wait_min_ms']})")
    print(f"Окно: поллинг с паузой {window['poll_interval_ms']} мс — ожидание "
          f"{window['poll_wait_ms']} мс")
    file_stats = report["file"]
    print(f"Файл: событие ReadDirectoryChangesW — {file_stats['event_ms']} мс, поллинг "
          f"(пауза {file_stats['poll_interval_ms']} мс, отсчёт от создания файла) — "
          f"{file_stats['polling_ms']} мс")
    print(f"Результат: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
