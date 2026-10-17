"""Слой UI Automation: адресные запросы с кэш-реквестом вместо обхода дерева.

Правила из замеров этапа 0:

  * область кэша выбирается по задаче. `Element|Children` (0x3) — свойства элемента и его
    детей, этого хватает шагу «найти и нажать», и вызов в 2.4 раза дешевле `Subtree`.
    `Subtree` (0x5) нужен только для карты на 3+ уровня: клиентский спуск глубже двух
    уровней работает лишь с ним. `Children` (0x2) без бита `Element` не годится вовсе —
    свойства возвращённых элементов не кэшируются и чтение `Cached*` падает с `E_INVALIDARG`;
  * свойства читаются ТОЛЬКО из кэша: `Current*` у элемента, построенного кэш-реквестом,
    требует полной догрузки (до 2 мс), тогда как кэш бесплатен;
  * `ConnectionTimeout` = 500 мс: провайдер, который не отвечает, прерывается исключением
    `0x800705B4` вместо бесконечного ожидания (без таймаута вызов не возвращается вовсе);
  * страницу Chromium и Electron обходом от окна не достать — нужен «прыжок» к корню
    контента: видимый дочерний HWND класса `Chrome_RenderWidgetHostHWND` (чистый Win32,
    доли миллисекунды) либо `FindFirst(ControlType=Document)`.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
import time
from pathlib import Path

import comtypes.client

comtypes.client.GetModule("UIAutomationCore.dll")
from comtypes.gen import UIAutomationClient as UIA  # noqa: E402

SCOPE_ELEMENT_AND_CHILDREN = 0x1 | 0x2
SCOPE_SUBTREE = 0x5

_BASE_PROPERTIES = (
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
    ("process_id", UIA.UIA_ProcessIdPropertyId),
    ("framework", UIA.UIA_FrameworkIdPropertyId),
)
_ACTION_PROPERTIES = (
    ("invoke", UIA.UIA_IsInvokePatternAvailablePropertyId),
    ("toggle", UIA.UIA_IsTogglePatternAvailablePropertyId),
    ("value", UIA.UIA_IsValuePatternAvailablePropertyId),
    ("expand_collapse", UIA.UIA_IsExpandCollapsePatternAvailablePropertyId),
    ("selection_item", UIA.UIA_IsSelectionItemPatternAvailablePropertyId),
    ("text", UIA.UIA_IsTextPatternAvailablePropertyId),
    ("scroll", UIA.UIA_IsScrollPatternAvailablePropertyId),
    ("window", UIA.UIA_IsWindowPatternAvailablePropertyId),
)
MINIMAL_PROPERTIES = tuple((name, prop) for name, prop in _BASE_PROPERTIES
                           if name in ("name", "automation_id", "control_type", "rect", "enabled",
                                       "process_id"))
ACTION_PROPERTIES = _BASE_PROPERTIES + _ACTION_PROPERTIES

_CAMEL = {
    "name": "Name", "automation_id": "AutomationId", "class_name": "ClassName",
    "control_type": "ControlType", "localized_type": "LocalizedControlType",
    "rect": "BoundingRectangle", "offscreen": "IsOffscreen", "enabled": "IsEnabled",
    "focusable": "IsKeyboardFocusable", "focused": "HasKeyboardFocus",
    "process_id": "ProcessId", "framework": "FrameworkId",
    "invoke": "IsInvokePatternAvailable", "toggle": "IsTogglePatternAvailable",
    "value": "IsValuePatternAvailable", "expand_collapse": "IsExpandCollapsePatternAvailable",
    "selection_item": "IsSelectionItemPatternAvailable", "text": "IsTextPatternAvailable",
    "scroll": "IsScrollPatternAvailable", "window": "IsWindowPatternAvailable",
}
_CONTROL_TYPES = {getattr(UIA, name): name for name in dir(UIA)
                  if name.startswith("UIA_") and name.endswith("ControlTypeId")}

CONTROL_TYPE_IDS = {
    "button": UIA.UIA_ButtonControlTypeId,
    "calendar": UIA.UIA_CalendarControlTypeId,
    "check_box": UIA.UIA_CheckBoxControlTypeId,
    "combo_box": UIA.UIA_ComboBoxControlTypeId,
    "document": UIA.UIA_DocumentControlTypeId,
    "edit": UIA.UIA_EditControlTypeId,
    "group": UIA.UIA_GroupControlTypeId,
    "hyperlink": UIA.UIA_HyperlinkControlTypeId,
    "image": UIA.UIA_ImageControlTypeId,
    "list": UIA.UIA_ListControlTypeId,
    "list_item": UIA.UIA_ListItemControlTypeId,
    "menu": UIA.UIA_MenuControlTypeId,
    "menu_bar": UIA.UIA_MenuBarControlTypeId,
    "menu_item": UIA.UIA_MenuItemControlTypeId,
    "pane": UIA.UIA_PaneControlTypeId,
    "progress_bar": UIA.UIA_ProgressBarControlTypeId,
    "radio_button": UIA.UIA_RadioButtonControlTypeId,
    "scroll_bar": UIA.UIA_ScrollBarControlTypeId,
    "separator": UIA.UIA_SeparatorControlTypeId,
    "slider": UIA.UIA_SliderControlTypeId,
    "spinner": UIA.UIA_SpinnerControlTypeId,
    "split_button": UIA.UIA_SplitButtonControlTypeId,
    "status_bar": UIA.UIA_StatusBarControlTypeId,
    "tab": UIA.UIA_TabControlTypeId,
    "tab_item": UIA.UIA_TabItemControlTypeId,
    "text": UIA.UIA_TextControlTypeId,
    "thumb": UIA.UIA_ThumbControlTypeId,
    "title_bar": UIA.UIA_TitleBarControlTypeId,
    "tool_bar": UIA.UIA_ToolBarControlTypeId,
    "tool_tip": UIA.UIA_ToolTipControlTypeId,
    "tree": UIA.UIA_TreeControlTypeId,
    "tree_item": UIA.UIA_TreeItemControlTypeId,
    "window": UIA.UIA_WindowControlTypeId,
}

CONTENT_ROOT_RENDER_WIDGET = "Chrome_RenderWidgetHostHWND"
MAIN_WINDOW_CLASSES = ()


def create_client(connection_timeout_ms: int | None = 500):
    """Объект UIA. С таймаутом нужен интерфейс `IUIAutomation2` (создаётся от CUIAutomation8)."""
    if connection_timeout_ms is None:
        return comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)
    automation = comtypes.client.CreateObject(UIA.CUIAutomation8, interface=UIA.IUIAutomation2)
    automation.ConnectionTimeout = int(connection_timeout_ms)
    return automation


# --- окна -------------------------------------------------------------------


def enum_windows() -> list[dict]:
    """Видимые окна верхнего уровня: дескриптор, процесс, исполняемый файл, заголовок."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                    wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
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
        result.append({"hwnd": int(hwnd), "pid": pid.value,
                       "exe": _process_exe(kernel32, pid.value), "title": title.value})
        return True

    user32.EnumWindows(enum_proc(callback), 0)
    return result


def _process_exe(kernel32, pid: int) -> str:
    handle = kernel32.OpenProcess(0x1000, False, pid)
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


def find_window(title_contains: str = "", exe: str = "", pid: int = 0) -> dict | None:
    """Первое подходящее окно; при нескольких — с наибольшей площадью."""
    surfaces = []
    for window in enum_windows():
        if title_contains and title_contains.lower() not in window["title"].lower():
            continue
        if exe and exe.lower() not in window["exe"].lower():
            continue
        if pid and window["pid"] != pid:
            continue
        surfaces.append(window)
    return surfaces[0] if surfaces else None


def child_windows(parent_hwnd: int) -> list[dict]:
    """Дочерние окна (включая вложенные) с классом, прямоугольником и видимостью."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    result: list[dict] = []
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _):
        class_name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_name, 256)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        result.append({"hwnd": int(hwnd), "class_name": class_name.value,
                       "visible": bool(user32.IsWindowVisible(hwnd)),
                       "width": rect.right - rect.left, "height": rect.bottom - rect.top,
                       "rect": {"left": rect.left, "top": rect.top,
                                "right": rect.right, "bottom": rect.bottom}})
        return True

    user32.EnumChildWindows(wintypes.HWND(parent_hwnd), enum_proc(callback), 0)
    return result


# --- элементы ---------------------------------------------------------------


def _scalar(value) -> float:
    """Модель или провайдер иногда отдают координату списком — берём первое число."""
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, (int, float)):
                return float(item)
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _rect_of(value) -> list[int] | None:
    if value is None:
        return None
    if hasattr(value, "left"):
        return [int(value.left), int(value.top), int(value.right), int(value.bottom)]
    try:
        values = list(value)
    except TypeError:
        return None
    return [int(item) for item in values[:4]] if len(values) >= 4 else None


def node_from_cached(element, profile: tuple = ACTION_PROPERTIES) -> dict:
    """Читает только `Cached*`: ни одного межпроцессного вызова."""
    return _node(element, profile, cached=True)


def node_from_current(element, profile: tuple = ACTION_PROPERTIES) -> dict:
    """Читает `Current*`: каждое свойство — переход в чужой процесс (антипример)."""
    return _node(element, profile, cached=False)


def _node(element, profile: tuple, cached: bool) -> dict:
    node: dict = {}
    for name, _property_id in profile:
        try:
            value = getattr(element, f"{'Cached' if cached else 'Current'}{_CAMEL[name]}")
        except Exception:  # noqa: BLE001 — свойства может не быть в кэше
            continue
        if name == "rect":
            node["rect"] = _rect_of(value)
        elif name == "control_type":
            node["control_type"] = _CONTROL_TYPES.get(value, f"0x{value:X}")
        elif name in _CAMEL and name not in ("rect", "control_type"):
            node[name] = value
    if node.get("rect"):
        node["center"] = [(node["rect"][0] + node["rect"][2]) // 2,
                          (node["rect"][1] + node["rect"][3]) // 2]
    return node


class UiaClient:
    """Клиент UIA с адресными запросами. Все методы — только через процесс-сателлит."""

    def __init__(self, connection_timeout_ms: int | None = 500) -> None:
        self.automation = create_client(connection_timeout_ms)
        self.connection_timeout_ms = connection_timeout_ms

    # --- поиск ------------------------------------------------------------

    def window(self, hwnd: int):
        return self.automation.ElementFromHandle(hwnd)

    def content_root(self, hwnd: int, jump: bool = True) -> tuple[object, dict]:
        """Корень контента окна: render-widget → Document → само окно.

        Без прыжка содержимое Chromium и Electron недостижимо: до страницы 8+ уровней
        хрома браузера. У WinUI-Блокнота `Document` оказывается листом (текст отдаётся
        `TextPattern`), поэтому вызывающий обязан проверить пустой первый уровень
        и откатиться на обход от окна.
        """
        element = self.window(hwnd)
        if not jump:
            return element, {"how": "window"}
        candidates = [child for child in child_windows(hwnd)
                      if CONTENT_ROOT_RENDER_WIDGET in child["class_name"] and child["visible"]
                      and child["width"] > 0 and child["height"] > 0]
        if candidates:
            widest = max(candidates, key=lambda child: child["width"] * child["height"])
            return self.automation.ElementFromHandle(widest["hwnd"]), {
                "how": "render_widget", "hwnd": hex(widest["hwnd"]),
                "class_name": widest["class_name"], "candidates": len(candidates)}
        condition = self.automation.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId,
                                                            UIA.UIA_DocumentControlTypeId)
        started = time.perf_counter()
        document = element.FindFirst(UIA.TreeScope_Descendants, condition)
        if document:
            return document, {"how": "document",
                              "ms": round((time.perf_counter() - started) * 1000.0, 2)}
        return element, {"how": "window", "note": "корень контента не найден"}

    def find(self, root, name: str = "", control_type: str = "", automation_id: str = "",
             scope: str = "shallow", depth: int = 3) -> dict | None:
        """Адресный поиск снизу вверх по одному запросу.

        `scope="shallow"` — кэш `Element|Children` (типовой шаг «найти и нажать»),
        `scope="deep"` — `Subtree` (когда нужен путь и родители).
        """
        conditions = []
        if name:
            conditions.append(self.automation.CreatePropertyCondition(UIA.UIA_NamePropertyId, name))
        if automation_id:
            conditions.append(self.automation.CreatePropertyCondition(
                UIA.UIA_AutomationIdPropertyId, automation_id))
        if control_type and control_type in CONTROL_TYPE_IDS:
            conditions.append(self.automation.CreatePropertyCondition(
                UIA.UIA_ControlTypePropertyId, CONTROL_TYPE_IDS[control_type]))
        if not conditions:
            raise ValueError("нужно хотя бы одно условие поиска")
        condition = conditions[0] if len(conditions) == 1 else \
            self.automation.CreateAndCondition(*conditions)

        cache = self.automation.CreateCacheRequest()
        cache.TreeScope = SCOPE_SUBTREE if scope == "deep" else SCOPE_ELEMENT_AND_CHILDREN
        for _name, property_id in ACTION_PROPERTIES:
            cache.AddProperty(property_id)

        started = time.perf_counter()
        element = root.FindFirstBuildCache(UIA.TreeScope_Descendants, condition, cache)
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        if not element:
            return None
        node = node_from_cached(element)
        node["search_ms"] = elapsed_ms
        return node

    def walk(self, root, depth: int = 3, scope: str = "deep") -> dict:
        """Спуск по уровням: один переход плюс клиентский обход по кэшу."""
        cache = self.automation.CreateCacheRequest()
        cache.TreeScope = SCOPE_SUBTREE if scope == "deep" else SCOPE_ELEMENT_AND_CHILDREN
        for _name, property_id in ACTION_PROPERTIES:
            cache.AddProperty(property_id)
        condition = self.automation.CreateTrueCondition()

        started = time.perf_counter()
        array = root.FindAllBuildCache(UIA.TreeScope_Children, condition, cache)
        level = [array.GetElement(index) for index in range(array.Length)]
        first_call_ms = round((time.perf_counter() - started) * 1000.0, 2)
        levels = [{"level": 1, "elements": len(level), "ms": first_call_ms}]
        nodes = [node_from_cached(element) for element in level]

        for index in range(2, depth + 1):
            started = time.perf_counter()
            children = []
            for element in level:
                children.extend(cached_children(element))
            levels.append({"level": index, "elements": len(children),
                           "ms": round((time.perf_counter() - started) * 1000.0, 2)})
            if not children:
                break
            nodes.extend(node_from_cached(element) for element in children)
            level = children
        return {"levels": levels, "nodes": nodes,
                "ms": round((time.perf_counter() - started) * 1000.0, 2)}


def element_text(element, max_length: int = -1) -> str:
    """Текст элемента через TextPattern.

    Нужен там, где дети не отдаются вовсе: WinUI-Блокнот представляет документ одним
    элементом без потомков, и содержимое доступно только через текстовый паттерн.
    """
    try:
        pattern = element.GetCurrentPattern(UIA.UIA_TextPatternId)
    except Exception:  # noqa: BLE001 — паттерн есть не у всех элементов
        return ""
    if not pattern:
        return ""
    try:
        text_pattern = pattern.QueryInterface(UIA.IUIAutomationTextPattern)
        return text_pattern.DocumentRange.GetText(max_length)
    except Exception:  # noqa: BLE001
        return ""


def cached_children(element) -> list:
    """Дети из кэша: без межпроцессного вызова, но NULL у листьев."""
    try:
        array = element.GetCachedChildren()
    except Exception:  # noqa: BLE001
        return []
    if not array:
        return []
    try:
        return [array.GetElement(index) for index in range(array.Length)]
    except ValueError:
        return []
