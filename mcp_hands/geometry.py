"""CoordinateSpace: единственный переводчик между тремя системами координат.

Системы координат, с которыми работает агент:

  * **координаты глазами процесса** — то, что возвращают UIA `BoundingRectangle`
    и Win32 `GetWindowRect`. У процесса без DPI-awareness Windows виртуализует их
    (на масштабе 125% экран выглядит как 1536×864 вместо 1920×1080);
  * **физические пиксели** — то, что даёт кадр DXGI и что видит пользователь;
  * **нормализованные 0..65535** — то, что ждёт `SendInput` с `MOUSEEVENTF_ABSOLUTE`.

Виртуализация сама по себе согласована (координаты и метрики ужимаются одинаково),
а ломается смешение пространств: цель, найденная на кадре DXGI в физических пикселях
и нормализованная unaware-процессом, даёт промах ровно на коэффициент масштаба.
Поэтому первая же строка пакета включает Per-Monitor DPI Awareness V2, а весь перевод
идёт через этот модуль.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes

_AWARENESS_NAME = {
    0: "DPI_AWARENESS_INVALID",
    1: "SYSTEM_DPI_AWARE",
    2: "PER_MONITOR_DPI_AWARE",
}
_ERROR_ACCESS_DENIED = 5
_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)


def enable_dpi_awareness() -> str:
    """Включает Per-Monitor DPI Awareness V2 и возвращает имя действующего режима.

    Вызывать до создания окон и COM-объектов: режим фиксируется один раз на процесс,
    повторный вызов вернёт `ERROR_ACCESS_DENIED`, если режим уже установлен другим кодом.
    """
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    set_context = getattr(user32, "SetProcessDpiAwarenessContext", None)
    if set_context is not None:
        set_context.argtypes = [ctypes.c_void_p]
        set_context.restype = ctypes.c_bool
        if set_context(_PER_MONITOR_AWARE_V2):
            return "PER_MONITOR_AWARE_V2"
        if ctypes.get_last_error() != _ERROR_ACCESS_DENIED:
            set_context = None
    if set_context is None:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
        shcore.SetProcessDpiAwareness.restype = ctypes.c_long
        result = shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        if result != 0 and (result & 0xFFFFFFFF) != 0x80070005:
            raise OSError(f"SetProcessDpiAwareness failed: 0x{result & 0xFFFFFFFF:08X}")
    return process_awareness()["name"]


def process_awareness() -> dict:
    """Фактический режим DPI текущего процесса: код и имя."""
    code = -1
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        shcore.GetProcessDpiAwareness.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        shcore.GetProcessDpiAwareness.restype = ctypes.c_long
        value = ctypes.c_int(-1)
        if shcore.GetProcessDpiAwareness(None, ctypes.byref(value)) == 0:
            code = value.value
    except OSError:
        pass
    return {"code": code, "name": _AWARENESS_NAME.get(code, "UNKNOWN")}


def point_in_rect(x: int, y: int, rect: dict) -> bool:
    return rect["left"] <= x <= rect["right"] and rect["top"] <= y <= rect["bottom"]


def rect_center(rect: dict) -> tuple[int, int]:
    return (rect["left"] + rect["right"]) // 2, (rect["top"] + rect["bottom"]) // 2


def intersection_area(first: dict, second: dict) -> int:
    """Площадь пересечения двух прямоугольников (0, если не пересекаются)."""
    width = min(first["right"], second["right"]) - max(first["left"], second["left"])
    height = min(first["bottom"], second["bottom"]) - max(first["top"], second["top"])
    return max(0, width) * max(0, height)


def rect_area(rect: dict) -> int:
    return max(0, rect["right"] - rect["left"]) * max(0, rect["bottom"] - rect["top"])


def occlusion_report(rect: dict, above: list[tuple[int, str, dict]],
                     threshold: float = 0.2) -> dict:
    """Перекрыто ли окно — считается по окнам ВЫШЕ него в Z-порядке.

    Захват кадра читает прямоугольник ЭКРАНА, а не содержимое окна: если поверх лежит
    чужое окно, то и скриншот, и OCR отдают чужое содержимое (замер 19.10: «окно Opera»
    прочиталось как интерфейс ZCode, 209 слов не по теме). Поэтому «видно ли окно» —
    отдельный факт, и он обязан доезжать до модели.

    `above` — список (hwnd, заголовок, прямоугольник) окон выше целевого, сверху вниз.
    Доли пересечений суммируются (при нескольких наложениях возможен двойной счёт) и
    ограничиваются единицей; `covered` — когда суммарная доля выше порога.
    """
    area = rect_area(rect)
    if area <= 0:
        return {"covered": False, "share": 0.0, "by": [], "reason": "нулевая площадь окна"}
    offenders: list[dict] = []
    share = 0.0
    for hwnd, title, other in above:
        overlap = intersection_area(rect, other)
        if overlap <= 0:
            continue
        part = overlap / area
        share += part
        if part >= 0.02:
            offenders.append({"hwnd": int(hwnd), "title": (title or "")[:80],
                              "share": round(part, 2)})
    share = min(1.0, share)
    return {"covered": share >= threshold, "share": round(share, 2),
            "by": sorted(offenders, key=lambda item: -item["share"])[:3]}


_GW_HWNDNEXT = 2


def _user32():
    library = ctypes.WinDLL("user32", use_last_error=True)
    library.GetTopWindow.restype = wintypes.HWND
    library.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    library.GetWindow.restype = wintypes.HWND
    return library


def _window_rect(hwnd: int) -> dict:
    raw = wintypes.RECT()
    if not _user32().GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(raw)):
        return {"left": 0, "top": 0, "right": 0, "bottom": 0}
    return {"left": raw.left, "top": raw.top, "right": raw.right, "bottom": raw.bottom}


def windows_above(hwnd: int) -> list[tuple[int, str, dict]]:
    """Окна выше заданного в Z-порядке (сверху вниз): только видимые и не свёрнутые.

    Идём от самого верхнего окна вниз по `GetWindow(GW_HWNDNEXT)` и останавливаемся на
    целевом — всё, что собрано до него, и есть потенциальные перекрытия.
    """
    user32 = _user32()
    above: list[tuple[int, str, dict]] = []
    current = user32.GetTopWindow(None)
    while current:
        handle = int(current)
        if handle == int(hwnd):
            break
        if user32.IsWindowVisible(current) and not user32.IsIconic(current):
            length = user32.GetWindowTextLengthW(current)
            title = ""
            if length:
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(current, buffer, length + 1)
                title = buffer.value
            above.append((handle, title, _window_rect(handle)))
        current = user32.GetWindow(current, _GW_HWNDNEXT)
    return above


def window_occlusion(hwnd: int) -> dict:
    """Что реально видно в прямоугольнике окна: свёрнуто ли оно и кто его перекрывает."""
    user32 = _user32()
    facts: dict = {"minimized": bool(user32.IsIconic(wintypes.HWND(hwnd)))}
    facts.update(occlusion_report(_window_rect(hwnd), windows_above(hwnd)))
    return facts


class Monitor:
    """Монитор: прямоугольник в физических пикселях и его DPI."""

    def __init__(self, handle: int, rect: dict, dpi: int) -> None:
        self.handle = handle
        self.rect = rect
        self.dpi = dpi or 96

    @property
    def scale(self) -> float:
        return self.dpi / 96.0

    def to_dict(self) -> dict:
        return {"handle": self.handle, "rect": self.rect, "dpi": self.dpi,
                "scale": round(self.scale, 3)}


def _monitor_dpi(handle: int) -> int:
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        shcore.GetDpiForMonitor.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                            ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
        shcore.GetDpiForMonitor.restype = ctypes.c_long
        dx, dy = ctypes.c_uint(0), ctypes.c_uint(0)
        if shcore.GetDpiForMonitor(handle, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0:
            return int(dx.value)
    except OSError:
        pass
    return 96


class CoordinateSpace:
    """Снимок координатной обстановки: виртуальный рабочий стол и мониторы.

    Все координаты внутри — физические пиксели. Нормализация считается от левого верхнего
    угла виртуального экрана: на конфигурации с монитором слева от основного
    `SM_XVIRTUALSCREEN` отрицателен, и без этого смещения клик уходит не туда.
    """

    def __init__(self) -> None:
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._prepare_prototypes()
        self.awareness = process_awareness()
        self.monitors = self._enumerate_monitors()

    # --- инициализация ----------------------------------------------------

    def _prepare_prototypes(self) -> None:
        user32 = self.user32
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = ctypes.c_bool
        user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        user32.MonitorFromPoint.restype = wintypes.HANDLE
        user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.MonitorFromWindow.restype = wintypes.HANDLE
        user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.c_void_p, ctypes.c_void_p,
                                               wintypes.LPARAM]

    def _enumerate_monitors(self) -> list[Monitor]:
        result: list[Monitor] = []
        monitor_enum = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HANDLE, wintypes.HDC,
                                          ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

        def callback(handle, _hdc, _rect, _data):
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            if self.user32.GetMonitorInfoW(handle, ctypes.byref(info)):
                rect = {"left": info.rcMonitor.left, "top": info.rcMonitor.top,
                        "right": info.rcMonitor.right, "bottom": info.rcMonitor.bottom}
                result.append(Monitor(int(handle or 0), rect, _monitor_dpi(int(handle or 0))))
            return True

        self.user32.EnumDisplayMonitors(None, None, monitor_enum(callback), 0)
        result.sort(key=lambda monitor: monitor.rect["left"])
        return result

    # --- метрики ----------------------------------------------------------

    @property
    def virtual_rect(self) -> dict:
        """Виртуальный рабочий стол в физических пикселях (может начинаться с отрицательных)."""
        get = self.user32.GetSystemMetrics
        left, top = get(76), get(77)
        width, height = get(78), get(79)
        return {"left": left, "top": top, "right": left + width, "bottom": top + height}

    def to_normalized(self, x: int, y: int, rect: dict | None = None) -> tuple[int, int]:
        """Физические пиксели → 0..65535 для `SendInput` с `MOUSEEVENTF_ABSOLUTE|VIRTUALDESK`."""
        area = rect or self.virtual_rect
        width = max(1, area["right"] - area["left"] - 1)
        height = max(1, area["bottom"] - area["top"] - 1)
        # Ограничение обязательно: значение больше 65535 переполняет 16-битный регистр
        # мыши в SendInput, и курсор улетает непредсказуемо.
        return (max(0, min(65535, round((x - area["left"]) * 65535 / width))),
                max(0, min(65535, round((y - area["top"]) * 65535 / height))))

    def from_normalized(self, nx: int, ny: int, rect: dict | None = None) -> tuple[int, int]:
        """Обратный перевод: нормализованные → физические пиксели."""
        area = rect or self.virtual_rect
        width = max(1, area["right"] - area["left"] - 1)
        height = max(1, area["bottom"] - area["top"] - 1)
        return (area["left"] + round(nx * width / 65535),
                area["top"] + round(ny * height / 65535))

    def monitor_for_point(self, x: int, y: int) -> Monitor | None:
        handle = self.user32.MonitorFromPoint(wintypes.POINT(x, y), 2)  # NEAREST
        return self._match_monitor(int(handle or 0))

    def monitor_for_window(self, hwnd: int) -> Monitor | None:
        handle = self.user32.MonitorFromWindow(wintypes.HWND(hwnd), 2)
        return self._match_monitor(int(handle or 0))

    def _match_monitor(self, handle: int) -> Monitor | None:
        for monitor in self.monitors:
            if monitor.handle == handle:
                return monitor
        return self.monitors[0] if self.monitors else None

    # --- окна -------------------------------------------------------------

    def window_rect_raw(self, hwnd: int) -> dict:
        """Прямоугольник из `GetWindowRect`: включает невидимые поля тени DWM.

        Оставлен для диагностики: разницу с видимой рамкой полезно видеть в журнале.
        """
        raw = wintypes.RECT()
        if not self.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(raw)):
            raise OSError(f"GetWindowRect failed for hwnd={hwnd}")
        return {"left": raw.left, "top": raw.top, "right": raw.right, "bottom": raw.bottom}

    def window_frame(self, hwnd: int) -> dict:
        """Прямоугольник ВИДИМОЙ рамки окна в физических пикселях.

        `GetWindowRect` включает невидимые поля изменения размера (тень DWM) — на стенде
        это 9 px слева, справа и снизу; они попадают в кропы и портят координаты элементов.
        Поэтому основной путь — `DWMWA_EXTENDED_FRAME_BOUNDS`, а `GetWindowRect` остаётся
        откатом для систем, где DWM-вызов недоступен.
        """
        raw = wintypes.RECT()
        if not self.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(raw)):
            raise OSError(f"GetWindowRect failed for hwnd={hwnd}")
        try:
            dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
            dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                                     ctypes.c_void_p, wintypes.DWORD]
            dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
            frame = wintypes.RECT()
            if dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), _DWMWA_EXTENDED_FRAME_BOUNDS,
                                            ctypes.byref(frame),
                                            ctypes.sizeof(frame)) == 0 and frame.right > frame.left:
                return {"left": frame.left, "top": frame.top,
                        "right": frame.right, "bottom": frame.bottom}
        except OSError:
            pass
        return {"left": raw.left, "top": raw.top, "right": raw.right, "bottom": raw.bottom}

    def describe(self) -> dict:
        """Сводка обстановки — пишется в журнал запуска: без неё замеры не сравнить."""
        return {
            "awareness": self.awareness,
            "virtual_rect": self.virtual_rect,
            "monitors": [monitor.to_dict() for monitor in self.monitors],
        }


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
