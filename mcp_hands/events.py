"""Событийный слой: ожидание вместо пауз, в выделенном потоке с циклом сообщений.

Устройство продиктовано проверкой фактом: колбэк внепроцессного хука вызывается
**в том потоке, который поставил хук, и только внутри обработки сообщений**. Если поток
не разбирает очередь, событий не приходит вовсе (проверено: за 3 секунды ни одного).
Поэтому здесь отдельный поток, который ставит хуки, крутит `GetMessage`/`DispatchMessage`
и складывает события в потокобезопасную очередь; остальной код читает очередь и не знает
про Win32.

Замеры, задающие ожидания: доставка хука — доли миллисекунды (точность ограничена тиком
планировщика 15.625 мс), появление файла через `ReadDirectoryChangesW` — 0.4 мс против
14–63 мс у поллинга с паузой 50/200 мс.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import queue
import threading
import time
from dataclasses import dataclass, field

EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MINIMIZESTART = 0x0016
EVENT_OBJECT_SHOW = 0x8002
EVENT_OBJECT_CREATE = 0x8000
EVENT_OBJECT_NAMECHANGE = 0x800C
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
WM_QUIT = 0x0012
PM_REMOVE = 0x0001
FILE_NOTIFY_CHANGE_FILE_NAME = 0x00000001
FILE_LIST_DIRECTORY = 0x0001
FILE_SHARE_ALL = 0x00000007
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000

_WIN_EVENT_PROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                                     ctypes.c_long, ctypes.c_long, wintypes.DWORD, wintypes.DWORD)


@dataclass
class Event:
    """Одно событие Windows: что произошло, с каким окном и когда."""

    kind: int
    hwnd: int
    object_id: int
    process_id: int
    thread_id: int
    event_time_ms: int
    received_at: float
    title: str = ""
    class_name: str = ""
    latency_ms: float | None = None
    extra: dict = field(default_factory=dict)

    def describe(self) -> dict:
        names = {EVENT_SYSTEM_FOREGROUND: "foreground", EVENT_OBJECT_SHOW: "show",
                 EVENT_OBJECT_CREATE: "create", EVENT_OBJECT_NAMECHANGE: "name_change",
                 EVENT_SYSTEM_MINIMIZESTART: "minimize"}
        return {"kind": names.get(self.kind, hex(self.kind)), "hwnd": hex(self.hwnd),
                "title": self.title, "class_name": self.class_name,
                "pid": self.process_id, "latency_ms": self.latency_ms, **self.extra}


class EventPump:
    """Поток с циклом сообщений и очередью событий наружу."""

    def __init__(self, events: tuple[int, ...] = (EVENT_SYSTEM_FOREGROUND, EVENT_OBJECT_SHOW),
                 pid: int = 0, include_own_process: bool = False) -> None:
        self.events = events
        self.pid = pid
        self.include_own_process = include_own_process
        self.queue: queue.Queue[Event] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._thread_id: int = 0
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._callbacks: list = []
        self._hooks: list[int] = []
        self._errors: list[str] = []

    # --- жизненный цикл ---------------------------------------------------

    def start(self, timeout_s: float = 3.0) -> bool:
        if self._thread is not None:
            return True
        self._thread = threading.Thread(target=self._run, name="mcp-hands-events", daemon=True)
        self._thread.start()
        return self._ready.wait(timeout_s)

    def stop(self, timeout_s: float = 2.0) -> None:
        self._stop.set()
        if self._thread_id:
            ctypes.WinDLL("user32", use_last_error=True).PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout_s)
            self._thread = None

    def __enter__(self) -> "EventPump":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    # --- поток ------------------------------------------------------------

    def _run(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentThreadId.restype = ctypes.c_ulong
        kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE,
                                           _WIN_EVENT_PROC, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
        user32.SetWinEventHook.restype = wintypes.HANDLE
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                       ctypes.c_uint, ctypes.c_uint]
        user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]

        self._thread_id = kernel32.GetCurrentThreadId()
        flags = WINEVENT_OUTOFCONTEXT | (0 if self.include_own_process else WINEVENT_SKIPOWNPROCESS)

        def make_handler(kind: int):
            def handler(_hook, event, hwnd, object_id, _child, thread_id, event_time) -> None:
                if object_id != 0:  # только сами окна, не подэлементы
                    return
                title = ctypes.create_unicode_buffer(256)
                class_name = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, title, 256)
                user32.GetClassNameW(hwnd, class_name, 256)
                now = kernel32.GetTickCount64() & 0xFFFFFFFF
                thread_for_window = user32.GetWindowThreadProcessId(hwnd, None)
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                self.queue.put(Event(kind=event, hwnd=int(hwnd or 0), object_id=object_id,
                                     process_id=pid.value, thread_id=thread_for_window,
                                     event_time_ms=event_time, received_at=time.perf_counter(),
                                     title=title.value, class_name=class_name.value,
                                     latency_ms=float((now - event_time) & 0xFFFFFFFF)))
            return _WIN_EVENT_PROC(handler)

        for kind in self.events:
            callback = make_handler(kind)
            self._callbacks.append(callback)  # колбэк обязан жить, пока живёт хук
            hook = user32.SetWinEventHook(kind, kind, None, callback, self.pid, 0, flags)
            if hook:
                self._hooks.append(hook)
            else:
                self._errors.append(f"SetWinEventHook({hex(kind)}) вернул 0")
        self._ready.set()

        # Без этого цикла колбэки не вызываются вовсе — проверено замером.
        message = wintypes.MSG()
        while not self._stop.is_set():
            result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
            if result in (0, -1):
                break
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
        for hook in self._hooks:
            user32.UnhookWinEvent(hook)
        self._hooks = []

    # --- чтение -----------------------------------------------------------

    def wait(self, predicate=None, timeout_s: float = 5.0) -> Event | None:
        """Ждёт подходящее событие. Уже накопленные в очереди тоже проверяются."""
        deadline = time.perf_counter() + timeout_s
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return None
            try:
                event = self.queue.get(timeout=min(0.1, remaining))
            except queue.Empty:
                continue
            if predicate is None or predicate(event):
                return event

    def drain(self) -> list[Event]:
        items = []
        while True:
            try:
                items.append(self.queue.get_nowait())
            except queue.Empty:
                return items

    def wait_for_window(self, title_contains: str = "", class_contains: str = "",
                        timeout_s: float = 5.0) -> Event | None:
        """Ждёт появления окна: заменяет цикл с паузами в сценариях."""
        def predicate(event: Event) -> bool:
            if event.kind not in (EVENT_OBJECT_SHOW, EVENT_OBJECT_CREATE, EVENT_SYSTEM_FOREGROUND):
                return False
            if title_contains and title_contains.lower() not in event.title.lower():
                return False
            if class_contains and class_contains.lower() not in event.class_name.lower():
                return False
            return True

        return self.wait(predicate, timeout_s)

    def health(self) -> dict:
        return {"running": self._thread is not None and self._thread.is_alive(),
                "thread_id": self._thread_id, "hooks": len(self._hooks),
                "errors": self._errors, "pending": self.queue.qsize()}


class FileWatcher:
    """Ожидание появления или изменения файла через `ReadDirectoryChangesW`."""

    def __init__(self, directory: str) -> None:
        self.directory = str(directory)
        self._result: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._handle = None

    def start(self) -> bool:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                         ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                         wintypes.HANDLE]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.ReadDirectoryChangesW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                                   wintypes.BOOL, wintypes.DWORD,
                                                   ctypes.POINTER(wintypes.DWORD),
                                                   ctypes.c_void_p, ctypes.c_void_p]
        handle = kernel32.CreateFileW(self.directory, FILE_LIST_DIRECTORY, FILE_SHARE_ALL, None,
                                      OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None)
        if handle == wintypes.HANDLE(-1).value:
            return False
        self._handle = handle
        self._thread = threading.Thread(target=self._run, args=(kernel32,), daemon=True)
        self._thread.start()
        return True

    def _run(self, kernel32) -> None:
        buffer = ctypes.create_string_buffer(4096)
        returned = wintypes.DWORD(0)
        while not self._stop.is_set():
            if kernel32.ReadDirectoryChangesW(self._handle, buffer, ctypes.sizeof(buffer), False,
                                              FILE_NOTIFY_CHANGE_FILE_NAME, ctypes.byref(returned),
                                              None, None) and returned.value:
                names = _parse_file_notifications(buffer.raw[: returned.value])
                for name in names:
                    self._result.put(name)

    def wait(self, timeout_s: float = 5.0) -> str | None:
        try:
            return self._result.get(timeout=timeout_s)
        except queue.Empty:
            return None

    def wait_for(self, name_contains: str, timeout_s: float = 5.0) -> str | None:
        deadline = time.perf_counter() + timeout_s
        while time.perf_counter() < deadline:
            name = self.wait(timeout_s=deadline - time.perf_counter())
            if name is None:
                return None
            if name_contains.lower() in name.lower():
                return name
        return None

    def stop(self) -> None:
        """Останавливает наблюдение.

        Порядок важен: `CloseHandle` на каталоге с висящим `ReadDirectoryChangesW`
        блокируется в ядре до завершения операции (то есть потенциально навсегда).
        Поэтому сначала `CancelIoEx` — операция завершается ошибкой, поток выходит
        из цикла, и только потом дескриптор закрывается.
        """
        self._stop.set()
        if self._handle:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
            kernel32.CancelIoEx.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CancelIoEx(self._handle, None)
            if self._thread is not None:
                self._thread.join(1.0)
            kernel32.CloseHandle(self._handle)
            self._handle = None
        if self._thread is not None:
            self._thread.join(1.0)
            self._thread = None

    def __enter__(self) -> "FileWatcher":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()


def _parse_file_notifications(raw: bytes) -> list[str]:
    """Разбор буфера FILE_NOTIFY_INFORMATION: имена файлов в UTF-16."""
    names: list[str] = []
    offset = 0
    while offset < len(raw):
        next_entry = int.from_bytes(raw[offset:offset + 4], "little")
        length = int.from_bytes(raw[offset + 8:offset + 12], "little")
        if length <= 0:
            break
        name = raw[offset + 12:offset + 12 + length].decode("utf-16-le", errors="replace")
        names.append(name)
        if not next_entry:
            break
        offset += next_entry
    return names
