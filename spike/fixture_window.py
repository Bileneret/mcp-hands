"""Тестовое окно для проверки координат, кликов и ожиданий: нативный Win32 с тремя кнопками.

Кнопки — настоящие Win32-контролы, поэтому видны в UIA (`ControlType=Button`, `Name`),
чего нельзя сказать о виджетах Tkinter: те через мост MSAA не отдают элементы, и проверять
на них перевод координат бессмысленно.

Каждое попадание по кнопке и каждый промах по окну пишутся строкой в файл-журнал:
    CLICK alpha
    MISS x=137 y=222
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wintypes
import os
import sys
from pathlib import Path

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_LBUTTONDOWN = 0x0201
WM_CLOSE = 0x0010
WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000
WS_BORDER = 0x00800000
BS_PUSHBUTTON = 0x00000000
IDC_ARROW = 32512

# Геометрия подобрана под замер CoordinateSpace: при ошибке масштаба 1.25 (клик из
# процесса без DPI-awareness) все промахи остаются ВНУТРИ этого окна и не задевают
# чужие приложения, но гарантированно не попадают в кнопки.
BUTTONS = (("alpha", 101, 20, 20, 200, 90),
           ("beta", 102, 300, 200, 200, 90),
           ("gamma", 103, 120, 340, 200, 90))

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


# Сигнатуры объявлены явно: без них ctypes ужимает 64-битные LPARAM до int
# и DefWindowProcW падает с OverflowError прямо в оконной процедуре.
user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
user32.CreateWindowExW.restype = wintypes.HWND
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.LoadCursorW.restype = wintypes.HANDLE
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, ctypes.c_uint, ctypes.c_uint]
user32.GetMessageW.restype = ctypes.c_int
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE


_log_path: Path | None = None


def log(line: str) -> None:
    if _log_path is None:
        return
    with _log_path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def cursor_pos() -> tuple[int, int]:
    """Физические координаты курсора: сразу после клика это точка попадания."""
    point = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


def wnd_proc(hwnd, message, wparam, lparam):
    if message == WM_COMMAND:
        ident = wparam & 0xFFFF
        for name, button_id, *_ in BUTTONS:
            if ident == button_id:
                x, y = cursor_pos()
                log(f"CLICK {name} px={x},{y}")
    elif message == WM_LBUTTONDOWN:
        x, y = cursor_pos()
        log(f"MISS px={x},{y}")
    elif message == WM_CLOSE:
        user32.DestroyWindow(hwnd)
    elif message == WM_DESTROY:
        user32.PostQuitMessage(0)
    return user32.DefWindowProcW(hwnd, message, wparam, lparam)


def main() -> int:
    global _log_path
    # Мишень обязана быть per-monitor aware: unaware-процесс Windows сама растягивает
    # через DWM, и в тесте координат появляется лишний слой виртуализации.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except OSError:
        pass
    parser = argparse.ArgumentParser(description="Тестовое окно с тремя кнопками")
    parser.add_argument("--click-log", default=str(Path(__file__).resolve().parent / ".clicks.log"))
    parser.add_argument("--title", default="MCP-Hands CoordinateSpace fixture")
    parser.add_argument("--x", type=int, default=140)
    parser.add_argument("--y", type=int, default=140)
    parser.add_argument("--width", type=int, default=900)
    parser.add_argument("--height", type=int, default=560)
    args = parser.parse_args()
    _log_path = Path(args.click_log)

    instance = kernel32.GetModuleHandleW(None)
    class_name = "McpHandsFixtureWindow"
    wnd_class = WNDCLASSW()
    wnd_class.lpfnWndProc = WNDPROC(wnd_proc)
    wnd_class.hInstance = instance
    wnd_class.hCursor = user32.LoadCursorW(None, ctypes.cast(IDC_ARROW, wintypes.LPCWSTR))
    wnd_class.hbrBackground = ctypes.cast(16, wintypes.HBRUSH)  # COLOR_BTNFACE + 1
    wnd_class.lpszClassName = class_name
    if not user32.RegisterClassW(ctypes.byref(wnd_class)):
        if ctypes.get_last_error() != 1410:  # ERROR_CLASS_ALREADY_EXISTS — повторный запуск
            raise OSError(f"RegisterClassW: {ctypes.get_last_error()}")

    hwnd = user32.CreateWindowExW(
        0, class_name, args.title,
        WS_OVERLAPPEDWINDOW | WS_VISIBLE,
        args.x, args.y, args.width, args.height, None, None, instance, None)
    if not hwnd:
        raise OSError(f"CreateWindowExW: {ctypes.get_last_error()}")

    for name, button_id, x, y, width, height in BUTTONS:
        user32.CreateWindowExW(0, "BUTTON", name, WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON,
                               x, y, width, height, hwnd, button_id, instance, None)

    print(f"HWND={hwnd:#x}", flush=True)
    print(f"PID={os.getpid()}", flush=True)
    print(f"BUTTONS={','.join(name for name, *_ in BUTTONS)}", flush=True)

    message = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(message))
        user32.DispatchMessageW(ctypes.byref(message))
    return 0


if __name__ == "__main__":
    sys.exit(main())
