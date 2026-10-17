"""Окно класса `#32770` — так выглядят модальные диалоги Windows.

Нужно тестам, которые проверяют сторож модальных окон: настоящий диалог Word по команде
не поднять, а класс окна — это то, по чему сторож и различает диалоги. Окно создаётся
и висит до закрытия процесса.

    python -m mcp_hands.testing.dialog_window --title "Файл заблокирован?"
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wintypes
import os

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
DIALOG_CLASS = "#32770"

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
user32.CreateWindowExW.restype = wintypes.HWND
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, ctypes.c_uint, ctypes.c_uint]
user32.GetMessageW.restype = ctypes.c_int
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE


def wnd_proc(hwnd, message, wparam, lparam):
    if message == WM_CLOSE:
        user32.DestroyWindow(hwnd)
    elif message == WM_DESTROY:
        user32.PostQuitMessage(0)
    return user32.DefWindowProcW(hwnd, message, wparam, lparam)


def main() -> int:
    parser = argparse.ArgumentParser(description="Окно модального диалога (#32770)")
    parser.add_argument("--title", default="Файл используется другим приложением?")
    parser.add_argument("--x", type=int, default=300)
    parser.add_argument("--y", type=int, default=300)
    args = parser.parse_args()

    instance = kernel32.GetModuleHandleW(None)
    wnd_class = WNDCLASSW()
    wnd_class.lpfnWndProc = WNDPROC(wnd_proc)
    wnd_class.hInstance = instance
    wnd_class.lpszClassName = DIALOG_CLASS
    if not user32.RegisterClassW(ctypes.byref(wnd_class)) and ctypes.get_last_error() != 1410:
        raise OSError(f"RegisterClassW: {ctypes.get_last_error()}")

    hwnd = user32.CreateWindowExW(0, DIALOG_CLASS, args.title,
                                  WS_OVERLAPPEDWINDOW | WS_VISIBLE,
                                  args.x, args.y, 420, 160, None, None, instance, None)
    if not hwnd:
        raise OSError(f"CreateWindowExW: {ctypes.get_last_error()}")
    print(f"HWND={hwnd:#x}", flush=True)
    print(f"PID={os.getpid()}", flush=True)
    print(f"CLASS={DIALOG_CLASS}", flush=True)

    message = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(message))
        user32.DispatchMessageW(ctypes.byref(message))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
