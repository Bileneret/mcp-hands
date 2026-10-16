"""Per-Monitor DPI Awareness V2 — обязательная первая строка координатных процессов.

Windows фиксирует режим DPI-awareness один раз на процесс: после первого созданного
окна или первого обращения к UIA сменить его уже нельзя (вызов вернёт E_ACCESSDENIED).
Поэтому `enable()` вызывается до всего остального — как в DESIGN.md §4.3.
"""

from __future__ import annotations

import ctypes

_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
_ERROR_ACCESS_DENIED = 5
_AWARENESS_NAME = {
    0: "DPI_AWARENESS_INVALID",
    1: "SYSTEM_DPI_AWARE",
    2: "PER_MONITOR_DPI_AWARE",
}


def enable() -> str:
    """Включает DPI-awareness и возвращает имя действующего режима.

    Сначала пробуем контекст V2 (Windows 10 1703+): он даёт корректные координаты
    у дочерних окон и диалогов на мониторах с разным масштабом. Если недоступен —
    откатываемся к `SetProcessDpiAwareness(2)`.
    """
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    set_ctx = getattr(user32, "SetProcessDpiAwarenessContext", None)
    if set_ctx is not None:
        set_ctx.argtypes = [ctypes.c_void_p]
        set_ctx.restype = ctypes.c_bool
        if set_ctx(_PER_MONITOR_AWARE_V2):
            return "PER_MONITOR_AWARE_V2"
        # ERROR_ACCESS_DENIED = режим уже зафиксирован ранее — это не ошибка вызова.
        if ctypes.get_last_error() != _ERROR_ACCESS_DENIED:
            set_ctx = None

    if set_ctx is None:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
        shcore.SetProcessDpiAwareness.restype = ctypes.c_long
        hr = shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        if hr != 0 and (hr & 0xFFFFFFFF) != 0x80070005:
            raise OSError(f"SetProcessDpiAwareness failed: 0x{hr & 0xFFFFFFFF:08X}")

    return report()["awareness"]


def report() -> dict:
    """Текущий режим DPI процесса, DPI монитора под курсором и масштаб в процентах."""
    awareness = "UNKNOWN"
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        value = ctypes.c_int(-1)
        shcore.GetProcessDpiAwareness.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        shcore.GetProcessDpiAwareness.restype = ctypes.c_long
        if shcore.GetProcessDpiAwareness(None, ctypes.byref(value)) == 0:
            awareness = _AWARENESS_NAME.get(value.value, str(value.value))
    except OSError:
        pass

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    return {"awareness": awareness, "monitor": _monitor_info(user32)}


def _monitor_info(user32) -> dict:
    """Монитор под курсором: физические координаты, DPI и масштаб."""
    from ctypes import wintypes

    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    monitor = user32.MonitorFromPoint(pt, 2)  # MONITOR_DEFAULTTONEAREST

    dpi_x = dpi_y = 0
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        dx, dy = ctypes.c_uint(0), ctypes.c_uint(0)
        shcore.GetDpiForMonitor.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)
        ]
        if shcore.GetDpiForMonitor(monitor, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0:
            dpi_x, dpi_y = dx.value, dy.value
    except OSError:
        pass

    if not dpi_x:
        dpi_x = dpi_y = 96
    return {
        "handle": monitor,
        "dpi": dpi_x,
        "scale_percent": round(dpi_x / 96 * 100),
        "cursor": (pt.x, pt.y),
    }


if __name__ == "__main__":
    mode = enable()
    info = report()
    print(f"DPI awareness: {mode} -> {info['awareness']}")
    print(f"Монитор под курсором: DPI {info['monitor']['dpi']} "
          f"({info['monitor']['scale_percent']}%), курсор {info['monitor']['cursor']}")
    print("Проверка после первого окна (режим уже зафиксирован):")
    import tkinter

    root = tkinter.Tk()
    root.withdraw()
    info2 = report()
    print(f"  повторный enable(): {enable()}")
    print(f"  состояние: {info2['awareness']}")
    root.destroy()
