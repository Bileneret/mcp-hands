"""Актуаторы: активация окна, клик в два шага с проверкой курсора, ввод текста.

Правила из замеров и аудита:

  * **Перед кликом окно активируется.** Первый клик по неактивному окну съедается
    активацией: нажатие до элемента не доходит вовсе;
  * **Клик выполняется в два шага**: перемещение → проверка позиции курсора
    (`GetCursorPos`) → нажатие. Пакет MOVE+DOWN+UP на неактивном окне смещает курсор
    (наблюдалось +29 px), а раздельная схема позволяет отменить нажатие, если курсор
    встал не туда;
  * абсолютные координаты помечаются `MOUSEEVENTF_VIRTUALDESK`: без него они проецируются
    только на первичный монитор;
  * **UIPI**: к окну процесса с более высокими правами `SendInput` и `SetForegroundWindow`
    молча не работают, без исключения в Python. Поэтому целостность проверяется заранее
    и отказ сообщается явно.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import time

from .geometry import CoordinateSpace, point_in_rect

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_MENU = 0x12

TOKEN_QUERY = 0x0008
TOKEN_INTEGRITY_LEVEL = 25
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

INTEGRITY_NAMES = {
    0x0000: "untrusted", 0x1000: "low", 0x2000: "medium", 0x2100: "medium-plus",
    0x3000: "high", 0x4000: "system",
}


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class INPUT(ctypes.Structure):
    class _UNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _UNION)]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", ctypes.c_ulong)]


class TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", SID_AND_ATTRIBUTES)]


def integrity_level_of_process(pid: int) -> dict:
    """Уровень целостности процесса: от него зависит, дойдёт ли до него ввод.

    Сигнатуры объявлены явно: без них ctypes усекает 64-битные дескрипторы до int
    и следующий же вызов падает с OverflowError вместо ответа.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                             wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
    process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not process:
        return {"level": None, "name": "неизвестно", "rid": None}
    token = wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(process, TOKEN_QUERY, ctypes.byref(token)):
            return {"level": None, "name": "неизвестно", "rid": None}
        size = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, buffer, size.value,
                                            ctypes.byref(size)):
            return {"level": None, "name": "неизвестно", "rid": None}
        label = ctypes.cast(buffer, ctypes.POINTER(TOKEN_MANDATORY_LABEL)).contents
        count = advapi32.GetSidSubAuthorityCount(label.Label.Sid).contents.value
        rid = advapi32.GetSidSubAuthority(label.Label.Sid, count - 1)
        value = int(rid.contents.value)
        return {"level": value, "rid": value,
                "name": INTEGRITY_NAMES.get(value & 0xF000, f"0x{value:X}")}
    finally:
        kernel32.CloseHandle(process)
        if token:
            kernel32.CloseHandle(token)


class Actuator:
    """Ввод в чужие окна: только через этот класс, иначе теряются проверки."""

    def __init__(self, space: CoordinateSpace | None = None) -> None:
        self.space = space or CoordinateSpace()
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._prepare_prototypes()

    def _prepare_prototypes(self) -> None:
        user32 = self.user32
        user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
        user32.SendInput.restype = ctypes.c_uint
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = ctypes.c_bool
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.AttachThreadInput.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_bool]
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(ctypes.c_ulong)]
        user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
        self.kernel32.GetCurrentThreadId.argtypes = []
        self.kernel32.GetCurrentThreadId.restype = ctypes.c_ulong
        self.kernel32.GetCurrentProcessId.argtypes = []
        self.kernel32.GetCurrentProcessId.restype = ctypes.c_ulong
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    # --- низкий уровень ---------------------------------------------------

    def _send(self, *spec) -> int:
        events = (INPUT * len(spec))()
        for index, (kind, flags, x, y, scan) in enumerate(spec):
            events[index].type = kind
            if kind == INPUT_MOUSE:
                if flags & MOUSEEVENTF_ABSOLUTE:
                    flags |= MOUSEEVENTF_VIRTUALDESK
                events[index].mi = MOUSEINPUT(x, y, 0, flags, 0, None)
            else:
                events[index].ki = KEYBDINPUT(0, scan, flags, 0, None)
        return self.user32.SendInput(len(spec), events, ctypes.sizeof(INPUT))

    def cursor(self) -> tuple[int, int]:
        point = wintypes.POINT()
        self.user32.GetCursorPos(ctypes.byref(point))
        return point.x, point.y

    def foreground(self) -> int:
        return int(self.user32.GetForegroundWindow() or 0)

    # --- активация --------------------------------------------------------

    def can_control(self, pid: int) -> dict:
        """Дойдёт ли ввод до процесса: сравниваем уровни целостности (UIPI)."""
        ours = integrity_level_of_process(self.kernel32.GetCurrentProcessId())
        theirs = integrity_level_of_process(pid)
        blocked = (ours["level"] is not None and theirs["level"] is not None
                   and theirs["level"] > ours["level"])
        return {"ours": ours, "target": theirs, "blocked_by_uipi": blocked}

    def activate(self, hwnd: int, timeout_s: float = 1.5) -> dict:
        """Делает окно активным. Прямой вызов часто отклоняется Windows, поэтому три приёма."""
        handle = wintypes.HWND(hwnd)
        if self.foreground() == hwnd:
            return {"ok": True, "how": "already"}
        self.user32.ShowWindow(handle, 9)  # SW_RESTORE
        deadline = time.perf_counter() + timeout_s
        while time.perf_counter() < deadline:
            current = self.kernel32.GetCurrentThreadId()
            foreground = self.foreground()
            other = self.user32.GetWindowThreadProcessId(wintypes.HWND(foreground), None) if foreground else 0
            attached = bool(other) and other != current
            if attached:
                self.user32.AttachThreadInput(current, other, True)
            try:
                self.user32.SetForegroundWindow(handle)
                if self.foreground() == hwnd:
                    return {"ok": True, "how": "attached" if attached else "direct"}
            finally:
                if attached:
                    self.user32.AttachThreadInput(current, other, False)
            self._tap_alt()  # снимает блокировку передачи фокуса
            time.sleep(0.15)
        return {"ok": self.foreground() == hwnd, "how": "timeout"}

    def _tap_alt(self) -> None:
        self._send((INPUT_KEYBOARD, 0, 0, 0, VK_MENU),
                   (INPUT_KEYBOARD, KEYEVENTF_KEYUP, 0, 0, VK_MENU))

    # --- клик -------------------------------------------------------------

    def move(self, x: int, y: int) -> tuple[int, int]:
        self._send((INPUT_MOUSE, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, *self.space.to_normalized(x, y), 0))

    def click(self, x: int, y: int, verify: bool = True, tolerance: int = 4,
              settle_s: float = 0.05) -> dict:
        """Перемещение, проверка позиции курсора, нажатие."""
        self.move(x, y)
        if settle_s:
            time.sleep(settle_s)
        landed = self.cursor()
        if verify and (abs(landed[0] - x) > tolerance or abs(landed[1] - y) > tolerance):
            return {"ok": False, "target": [x, y], "landed": list(landed),
                    "reason": "курсор не встал на цель — нажатие отменено"}
        sent = self._send((INPUT_MOUSE, MOUSEEVENTF_LEFTDOWN, 0, 0, 0),
                          (INPUT_MOUSE, MOUSEEVENTF_LEFTUP, 0, 0, 0))
        return {"ok": sent == 2, "target": [x, y], "landed": list(landed), "events": sent}

    def click_node(self, node: dict, pid: int = 0, activate: bool = True) -> dict:
        """Клик по элементу: активация окна, при необходимости проверка UIPI, клик."""
        rect = node.get("rect")
        if not rect:
            return {"ok": False, "reason": "у элемента нет прямоугольника"}
        center = node.get("center") or [(rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2]
        result = {"node": {"name": node.get("name"), "control_type": node.get("control_type"),
                           "rect": rect}, "center": center}
        if pid:
            rights = self.can_control(pid)
            result["rights"] = rights
            if rights["blocked_by_uipi"]:
                return {**result, "ok": False,
                        "reason": "целевое окно запущено с более высокими правами: ввод не дойдёт"}
        if activate and node.get("hwnd"):
            result["activation"] = self.activate(node["hwnd"])
        result.update(self.click(*center))
        return result

    # --- клавиатура -------------------------------------------------------

    def type_text(self, text: str, char_delay_s: float = 0.0) -> dict:
        """Ввод текста через SendInput в режиме Unicode: работает с кириллицей."""
        for char in text:
            code = ord(char)
            self._send((INPUT_KEYBOARD, KEYEVENTF_UNICODE, 0, 0, code),
                       (INPUT_KEYBOARD, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0, code))
            if char_delay_s:
                time.sleep(char_delay_s)
        return {"ok": True, "chars": len(text)}

    def hotkey(self, *virtual_keys: int) -> dict:
        for key in virtual_keys:
            self._send((INPUT_KEYBOARD, 0, 0, 0, key))
        for key in reversed(virtual_keys):
            self._send((INPUT_KEYBOARD, KEYEVENTF_KEYUP, 0, 0, key))
        return {"ok": True, "keys": list(virtual_keys)}

    def click_inside(self, rect: dict, x: int, y: int) -> bool:
        """Проверка, что точка внутри прямоугольника — защита от промаха по координатам."""
        return point_in_rect(x, y, rect)
