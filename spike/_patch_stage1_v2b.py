"""Патч по итогам диагностики: честная привязка к рабочему столу и подъём окна перед кликом."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def patch(relative: str, replacements: list[tuple[str, str]], note: str) -> None:
    path = ROOT / relative
    text = path.read_text(encoding="utf-8")
    for old, new in replacements:
        if old not in text:
            raise SystemExit(f"не найден фрагмент в {relative}: {old[:70]!r}")
        text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")
    print(f"{relative}: {note}")


patch("mcp_hands/input.py", [
    ('''        user32.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                     ctypes.c_void_p, wintypes.DWORD,
                                                     ctypes.POINTER(wintypes.DWORD)]
        user32.CloseDesktop.argtypes = [wintypes.HANDLE]

        desktop = user32.OpenInputDesktop(0, False, 0x01FF)  # DESKTOP_ALL_ACCESS
        if not desktop:
            self.desktop = {"attached": False, "reason": f"OpenInputDesktop: {ctypes.get_last_error()}"}
            return self.desktop
        buffer = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD(0)
        user32.GetUserObjectInformationW(desktop, 2, buffer, ctypes.sizeof(buffer),  # UOI_NAME
                                         ctypes.byref(needed))
        attached = bool(user32.SetThreadDesktop(desktop))
        self.desktop = {"attached": attached, "name": buffer.value,
                        "reason": None if attached else f"SetThreadDesktop: {ctypes.get_last_error()}"}
        return self.desktop''',
     '''        user32.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                     ctypes.c_void_p, wintypes.DWORD,
                                                     ctypes.POINTER(wintypes.DWORD)]
        user32.GetThreadDesktop.argtypes = [wintypes.DWORD]
        user32.GetThreadDesktop.restype = wintypes.HANDLE
        user32.CloseDesktop.argtypes = [wintypes.HANDLE]

        desktop = user32.OpenInputDesktop(0, False, 0x01FF)  # DESKTOP_ALL_ACCESS
        if not desktop:
            self.desktop = {"attached": False, "name": None,
                            "reason": f"OpenInputDesktop: {ctypes.get_last_error()}"}
            return self.desktop
        input_name = self._desktop_name(desktop)
        current = user32.GetThreadDesktop(self.kernel32.GetCurrentThreadId())
        current_name = self._desktop_name(current) if current else None

        # Переключаться нужно только если рабочие столы РАЗНЫЕ. Если поток уже на столе
        # ввода (обычный случай для интерактивной сессии), вызов SetThreadDesktop вернёт
        # ошибку 170 — поток успел создать скрытое окно (их создаёт comtypes для
        # маршалинга COM), и это не признак нерабочего ввода.
        if current_name and current_name == input_name:
            self.desktop = {"attached": True, "name": input_name, "how": "уже на столе ввода",
                            "reason": None}
            return self.desktop
        attached = bool(user32.SetThreadDesktop(desktop))
        self.desktop = {"attached": attached, "name": input_name,
                        "how": "переключён" if attached else "не удалось",
                        "thread_desktop": current_name,
                        "reason": None if attached else
                                  f"SetThreadDesktop: {ctypes.get_last_error()} "
                                  f"(170 — у потока уже есть окна или хуки)"}
        return self.desktop

    @staticmethod
    def _desktop_name(handle) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        needed = ctypes.wintypes.DWORD(0)
        ok = ctypes.WinDLL("user32", use_last_error=True).GetUserObjectInformationW(
            handle, 2, buffer, ctypes.sizeof(buffer), ctypes.byref(needed))  # UIO_NAME
        return buffer.value if ok else f"<ошибка {ctypes.get_last_error()}>"'''),
    ('''    def click_node(self, node: dict, pid: int = 0, activate: bool = True) -> dict:
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
        return result''',
     '''    def click_node(self, node: dict, pid: int = 0, activate: bool = True,
                   window_hwnd: int = 0) -> dict:
        """Клик по элементу: активация окна, проверка UIPI, клик.

        Окно поднимается ВСЕГДА, когда известен его дескриптор: элемент из UIA не несёт
        `hwnd` (это свойство окна, а не кнопки), и без явной активации клик уходит
        в перекрывающее окно — например, в консоль поверх мишени.
        """
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
        target_hwnd = window_hwnd or node.get("hwnd") or 0
        if activate and target_hwnd:
            result["activation"] = self.activate(target_hwnd)
        result.update(self.click(*center))
        return result'''),
], "сравнение рабочих столов вместо слепого переключения; подъём окна перед кликом")

patch("mcp_hands/probe_worker.py", [
    ('''        window = next((w for w in self.op_windows() if w["hwnd"] == hwnd), None)
        return self.actuator.click_node(node, pid=window["pid"] if window else 0)''',
     '''        window = next((w for w in self.op_windows() if w["hwnd"] == hwnd), None)
        return self.actuator.click_node(node, pid=window["pid"] if window else 0,
                                        window_hwnd=hwnd)'''),
], "окно цели передаётся в клик явно")

patch("tests/conftest.py", [
    ('''        time.sleep(0.6)  # окну нужно дорисоваться и попасть в дерево UIA''',
     '''        time.sleep(0.6)  # окну нужно дорисоваться и попасть в дерево UIA
        self.bring_to_front()

    def bring_to_front(self) -> bool:
        """Поднимает мишень: иначе её перекрывает постороннее окно и клик уходит не туда."""
        from mcp_hands.geometry import CoordinateSpace
        from mcp_hands.input import Actuator

        return bool(Actuator(CoordinateSpace()).activate(self.hwnd).get("ok"))'''),
    ('''@pytest.fixture
def target():
    """Окно-мишень с кнопками alpha, beta, gamma."""
    fixture = Target("mcp_hands.testing.fixture_window", "MCP-Hands pytest target")
    try:
        yield fixture
    finally:
        fixture.close()''',
     '''@pytest.fixture
def target():
    """Окно-мишень с кнопками alpha, beta, gamma.

    Перед каждым использованием поднимается наверх: в фоне могут появляться консольные
    окна от тестовых команд, и они перехватывают клики и закрывают часть мишени.
    """
    fixture = Target("mcp_hands.testing.fixture_window", "MCP-Hands pytest target")
    try:
        fixture.bring_to_front()
        yield fixture
    finally:
        fixture.close()'''),
], "мишень поднимается перед тестами")
