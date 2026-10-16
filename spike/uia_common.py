"""Общая обвязка UIA: объект IUIAutomation, кэш-реквесты, сериализация элемента.

Правила DESIGN.md §4.2 закодированы здесь:
  * все нужные свойства собираются одним кэш-реквестом (один межпроцессный переход);
  * область — TreeScope_Children, глубина ограничивается вызывающей стороной;
  * доступность действий читается булевыми свойствами `Is*PatternAvailable`,
    а не объектами паттернов: тот же ответ, но без создания COM-интерфейсов на узел.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os

import comtypes.client

comtypes.client.GetModule("UIAutomationCore.dll")
from comtypes.gen import UIAutomationClient as UIA  # noqa: E402

# --- свойства ---------------------------------------------------------------

BASE_PROPERTIES = (
    ("name", UIA.UIA_NamePropertyId),
    ("automation_id", UIA.UIA_AutomationIdPropertyId),
    ("class_name", UIA.UIA_ClassNamePropertyId),
    ("control_type", UIA.UIA_ControlTypePropertyId),
    ("localized_type", UIA.UIA_LocalizedControlTypePropertyId),
    ("rect", UIA.UIA_BoundingRectanglePropertyId),
    ("offscreen", UIA.UIA_IsOffscreenPropertyId),
    ("enabled", UIA.UIA_IsEnabledPropertyId),
    ("focusable", UIA.UIA_IsKeyboardFocusablePropertyId),
    ("focused", UIA.UIA_HasKeyboardFocusPropertyId),
    ("password", UIA.UIA_IsPasswordPropertyId),
    ("process_id", UIA.UIA_ProcessIdPropertyId),
    ("framework", UIA.UIA_FrameworkIdPropertyId),
    ("hwnd", UIA.UIA_NativeWindowHandlePropertyId),
)

# Доступность действий — булевы свойства, приходят в том же кэш-реквесте.
ACTION_PROPERTIES = (
    ("invoke", UIA.UIA_IsInvokePatternAvailablePropertyId),
    ("toggle", UIA.UIA_IsTogglePatternAvailablePropertyId),
    ("value", UIA.UIA_IsValuePatternAvailablePropertyId),
    ("expand_collapse", UIA.UIA_IsExpandCollapsePatternAvailablePropertyId),
    ("selection_item", UIA.UIA_IsSelectionItemPatternAvailablePropertyId),
    ("selection", UIA.UIA_IsSelectionPatternAvailablePropertyId),
    ("range_value", UIA.UIA_IsRangeValuePatternAvailablePropertyId),
    ("text", UIA.UIA_IsTextPatternAvailablePropertyId),
    ("scroll", UIA.UIA_IsScrollPatternAvailablePropertyId),
    ("window", UIA.UIA_IsWindowPatternAvailablePropertyId),
    ("transform", UIA.UIA_IsTransformPatternAvailablePropertyId),
    ("legacy", UIA.UIA_IsLegacyIAccessiblePatternAvailablePropertyId),
)

# Паттерны для режима --patterns full (замер стоимости AddPattern).
FULL_PATTERNS = (
    ("invoke", UIA.UIA_InvokePatternId),
    ("toggle", UIA.UIA_TogglePatternId),
    ("value", UIA.UIA_ValuePatternId),
    ("expand_collapse", UIA.UIA_ExpandCollapsePatternId),
    ("selection_item", UIA.UIA_SelectionItemPatternId),
    ("window", UIA.UIA_WindowPatternId),
)

CONTROL_TYPES = {
    getattr(UIA, name): name
    for name in dir(UIA)
    if name.startswith("UIA_") and name.endswith("ControlTypeId")
}

PROFILE_PROPERTIES = {
    "base": BASE_PROPERTIES,
    "action": BASE_PROPERTIES + ACTION_PROPERTIES,
    "full": BASE_PROPERTIES + ACTION_PROPERTIES,
}


def create_uia(connection_timeout_ms: int | None = None):
    """Создаёт объект UIA. Таймаут соединения доступен через IUIAutomation2 (CUIAutomation8)."""
    if connection_timeout_ms is None:
        return comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)
    uia = comtypes.client.CreateObject(UIA.CUIAutomation8, interface=UIA.IUIAutomation2)
    uia.ConnectionTimeout = int(connection_timeout_ms)
    return uia


def read_timeouts() -> dict:
    """Заводские таймауты UIA (актуальны только для CUIAutomation8)."""
    try:
        uia = comtypes.client.CreateObject(UIA.CUIAutomation8, interface=UIA.IUIAutomation2)
        return {"connection_ms": uia.ConnectionTimeout, "transaction_ms": uia.TransactionTimeout}
    except Exception as exc:  # noqa: BLE001 — диагностический помощник
        return {"error": str(exc)}


def make_cache(uia, scope: int, profile: str = "action", patterns: bool = False):
    """Кэш-реквест: набор свойств (и, опционально, паттернов) за один переход в чужой процесс."""
    cache = uia.CreateCacheRequest()
    cache.TreeScope = scope
    for _, prop_id in PROFILE_PROPERTIES[profile]:
        cache.AddProperty(prop_id)
    if patterns:
        for _, pattern_id in FULL_PATTERNS:
            cache.AddPattern(pattern_id)
    return cache


# --- сериализация -----------------------------------------------------------


def _rect_tuple(rect) -> list[int] | None:
    if rect is None:
        return None
    if hasattr(rect, "left"):
        return [rect.left, rect.top, rect.right, rect.bottom]
    try:
        values = list(rect)
    except TypeError:
        return None
    return [int(v) for v in values[:4]] if len(values) >= 4 else None


def node_from_cached(element, profile: str = "action") -> dict:
    """Читает только Cached*-свойства: ни одного межпроцессного вызова."""
    node: dict = {}
    for name, _ in PROFILE_PROPERTIES[profile]:
        try:
            value = getattr(element, f"Cached{_camel(name)}")
        except Exception:  # noqa: BLE001 — свойство могло не попасть в кэш
            continue
        if name == "rect":
            node["rect"] = _rect_tuple(value)
        elif name == "control_type":
            node["control_type"] = CONTROL_TYPES.get(value, f"0x{value:X}")
        elif name in ("name", "automation_id", "class_name", "localized_type", "framework"):
            node[name] = value or ""
        else:
            node[name] = value
    return node


_CAMEL = {
    "name": "Name",
    "automation_id": "AutomationId",
    "class_name": "ClassName",
    "control_type": "ControlType",
    "localized_type": "LocalizedControlType",
    "rect": "BoundingRectangle",
    "offscreen": "IsOffscreen",
    "enabled": "IsEnabled",
    "focusable": "IsKeyboardFocusable",
    "focused": "HasKeyboardFocus",
    "password": "IsPassword",
    "process_id": "ProcessId",
    "framework": "FrameworkId",
    "hwnd": "NativeWindowHandle",
    "invoke": "IsInvokePatternAvailable",
    "toggle": "IsTogglePatternAvailable",
    "value": "IsValuePatternAvailable",
    "expand_collapse": "IsExpandCollapsePatternAvailable",
    "selection_item": "IsSelectionItemPatternAvailable",
    "selection": "IsSelectionPatternAvailable",
    "range_value": "IsRangeValuePatternAvailable",
    "text": "IsTextPatternAvailable",
    "scroll": "IsScrollPatternAvailable",
    "window": "IsWindowPatternAvailable",
    "transform": "IsTransformPatternAvailable",
    "legacy": "IsLegacyIAccessiblePatternAvailable",
}


def _camel(name: str) -> str:
    return _CAMEL[name]


def node_from_current(element, profile: str = "action") -> dict:
    """Читает Current*-свойства: каждое обращение — отдельный межпроцессный вызов (антипример)."""
    node: dict = {}
    for name, _ in PROFILE_PROPERTIES[profile]:
        try:
            value = getattr(element, f"Current{_camel(name)}")
        except Exception:  # noqa: BLE001
            continue
        if name == "rect":
            node["rect"] = _rect_tuple(value)
        elif name == "control_type":
            node["control_type"] = CONTROL_TYPES.get(value, f"0x{value:X}")
        elif name in ("name", "automation_id", "class_name", "localized_type", "framework"):
            node[name] = value or ""
        else:
            node[name] = value
    return node


def node_brief(node: dict) -> str:
    """Однострочное представление узла для дампа в консоль."""
    rect = node.get("rect") or [0, 0, 0, 0]
    return "{:<18} {:<34} {} {} {:<22} {}".format(
        node.get("control_type", "?")[:18],
        (node.get("name") or "")[:34],
        f"{rect[0]:>5},{rect[1]:>5}",
        f"{rect[2] - rect[0]:>4}x{rect[3] - rect[1]:<4}",
        node.get("class_name", "")[:22],
        node.get("automation_id", "")[:20],
    )


def cached_children(element) -> list:
    """Дети из кэша элемента: без межпроцессного вызова, но NULL у листьев.

    `GetCachedChildren` возвращает NULL, если детей нет, — comtypes отдаёт на этом
    ValueError при обращении к `.Length`, поэтому выход пустым списком, а не исключением.
    """
    try:
        array = element.GetCachedChildren()
    except Exception:  # noqa: BLE001 — элемент мог быть создан без кэша детей
        return []
    if not array:
        return []
    try:
        return [array.GetElement(index) for index in range(array.Length)]
    except ValueError:
        return []


def cached_rect(node: dict, default=(0, 0, 0, 0)) -> tuple[int, int, int, int]:
    rect = node.get("rect") or list(default)
    return rect[0], rect[1], rect[2], rect[3]


# --- окна -------------------------------------------------------------------


def enum_windows() -> list[dict]:
    """Видимые окна верхнего уровня: hwnd, pid, exe, заголовок, прямоугольник."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    result: list[dict] = []

    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        result.append({
            "hwnd": int(hwnd),
            "pid": pid.value,
            "exe": _process_exe(kernel32, pid.value),
            "title": title.value,
        })
        return True

    user32.EnumWindows(enum_proc(callback), 0)
    return result


def _process_exe(kernel32, pid: int) -> str:
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buffer))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return os.path.basename(buffer.value)
    finally:
        kernel32.CloseHandle(handle)
    return ""


def child_windows(parent_hwnd: int) -> list[dict]:
    """Дочерние окна (включая вложенные) с классом, прямоугольником и видимостью.

    Нужны для «прыжка» к корню контента без обхода UIA-дерева: у Chromium страница
    живёт в отдельном HWND класса `Chrome_RenderWidgetHostHWND`, и Win32-перечисление
    находит его без единого межпроцессного вызова UIA.
    """
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    result: list[dict] = []
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _):
        class_name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_name, 256)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        result.append({
            "hwnd": int(hwnd),
            "class_name": class_name.value,
            "visible": bool(user32.IsWindowVisible(hwnd)),
            "width": rect.right - rect.left,
            "height": rect.bottom - rect.top,
            "rect": {"left": rect.left, "top": rect.top, "right": rect.right, "bottom": rect.bottom},
        })
        return True

    user32.EnumChildWindows(wintypes.HWND(parent_hwnd), enum_proc(callback), 0)
    return result


def find_windows(exe: str, title_substring: str = "") -> list[dict]:
    """Окна по имени процесса (без учёта регистра) и, опционально, подстроке заголовка."""
    exe_lower = exe.lower()
    found = []
    for item in enum_windows():
        if exe_lower not in item["exe"].lower():
            continue
        if title_substring and title_substring.lower() not in item["title"].lower():
            continue
        found.append(item)
    return found


def window_rect(hwnd: int) -> dict:
    """Прямоугольник окна в физических пикселях (GetWindowRect, без DWM-теней)."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    rect = wintypes.RECT()
    if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        raise OSError(f"GetWindowRect failed for hwnd={hwnd}")
    return {"left": rect.left, "top": rect.top, "right": rect.right, "bottom": rect.bottom}
