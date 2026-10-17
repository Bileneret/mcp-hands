"""Рабочая сторона процесса-сателлита: исполняет операции, которые могут зависнуть.

Запускается из `probe.ProbeProcess`, общается с родителем через канал `multiprocessing`.
Внутри — только тяжёлые и опасные вещи: UIA (чужой провайдер может подвиснуть),
COM-вызовы Office, физический ввод. Родитель убивает этот процесс по таймауту, поэтому
здесь нет ни сценариев, ни состояния, которое жалко потерять: перезапуск дешёвый
по сравнению с зависшим шагом агента.
"""

from __future__ import annotations

import ctypes
import time
import traceback


def _enable_dpi() -> str:
    with __import__("contextlib").suppress(OSError):
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    return "PER_MONITOR_DPI_AWARE"


class Worker:
    """Состояние сателлита: клиент UIA, актуатор и координатное пространство создаются один раз."""

    def __init__(self) -> None:
        self.awareness = _enable_dpi()
        self._uia = None
        self._actuator = None
        self._space = None
        self.started_at = time.time()

    # --- ленивая инициализация: не платим за то, что не понадобилось ---

    @property
    def uia(self):
        if self._uia is None:
            from .uia import UiaClient

            self._uia = UiaClient()
        return self._uia

    @property
    def actuator(self):
        if self._actuator is None:
            from .input import Actuator

            self._actuator = Actuator(self.space)
        return self._actuator

    @property
    def space(self):
        if self._space is None:
            from .geometry import CoordinateSpace

            self._space = CoordinateSpace()
        return self._space

    # --- операции ---------------------------------------------------------

    def handle(self, op: str, **kwargs) -> object:
        method = getattr(self, f"op_{op}", None)
        if method is None:
            raise ValueError(f"неизвестная операция: {op}")
        return method(**kwargs)

    def op_ping(self) -> dict:
        from . import __version__

        return {"version": __version__, "awareness": self.awareness,
                "pid": ctypes.windll.kernel32.GetCurrentProcessId()}

    def op_windows(self, title_contains: str = "", exe: str = "") -> list[dict]:
        from . import uia

        if title_contains or exe:
            window = uia.find_window(title_contains=title_contains, exe=exe)
            return [window] if window else []
        return uia.enum_windows()

    def op_element(self, hwnd: int, name: str = "", control_type: str = "",
                   automation_id: str = "", scope: str = "shallow") -> dict | None:
        root, info = self.uia.content_root(hwnd, jump=True)
        node = self.uia.find(root, name=name, control_type=control_type,
                             automation_id=automation_id, scope=scope)
        if node is None:
            return None
        node["content_root"] = info
        return node

    def op_walk(self, hwnd: int, depth: int = 3, jump: bool = True, scope: str = "deep") -> dict:
        root, info = self.uia.content_root(hwnd, jump=jump)
        result = self.uia.walk(root, depth=depth, scope=scope)
        result["content_root"] = info
        if jump and info.get("how") != "window" and result["levels"] and \
                result["levels"][0]["elements"] == 0:
            # Document оказался листом (WinUI-текстовое поле) — обходим от окна.
            result = self.uia.walk(self.uia.window(hwnd), depth=depth, scope=scope)
            result["content_root"] = {**info, "fallback": "document без детей, обход от окна"}
        return result

    def op_click(self, hwnd: int, name: str = "", control_type: str = "",
                 automation_id: str = "", move_only: bool = False) -> dict:
        node = self.op_element(hwnd=hwnd, name=name, control_type=control_type,
                               automation_id=automation_id)
        if node is None:
            return {"ok": False, "reason": "элемент не найден"}
        if move_only:
            center = node["center"]
            self.actuator.move(*center)
            time.sleep(0.05)
            landed = self.actuator.cursor()
            return {"ok": True, "moved_only": True, "center": center, "landed": list(landed)}
        window = next((w for w in self.op_windows() if w["hwnd"] == hwnd), None)
        return self.actuator.click_node(node, pid=window["pid"] if window else 0)

    def op_type(self, text: str) -> dict:
        return self.actuator.type_text(text)

    def op_hotkey(self, keys: list[int]) -> dict:
        return self.actuator.hotkey(*keys)

    def op_activate(self, hwnd: int) -> dict:
        return self.actuator.activate(hwnd)

    def op_rights(self, pid: int) -> dict:
        return self.actuator.can_control(pid)

    def op_space(self) -> dict:
        return self.space.describe()

    def op_sleep(self, seconds: float = 1.0) -> dict:
        """Диагностика: заведомо долгая операция. Нужна тестам пути «бюджет истёк → убийство»."""
        time.sleep(float(seconds))
        return {"slept": seconds}

    def op_stats(self) -> dict:
        return {"uptime_s": round(time.time() - self.started_at, 1),
                "has_uia": self._uia is not None, "has_actuator": self._actuator is not None}


def main(connection) -> None:
    """Цикл сателлита: читаем запрос, отвечаем, ждём следующий."""
    worker = Worker()
    try:
        while True:
            try:
                request = connection.recv()
            except (EOFError, OSError):
                return
            if request is None:
                return
            op = request.get("op", "")
            kwargs = request.get("kwargs", {})
            started = time.perf_counter()
            try:
                result = worker.handle(op, **kwargs)
                response = {"ok": True, "result": result,
                            "ms": round((time.perf_counter() - started) * 1000.0, 2)}
            except Exception as exc:  # noqa: BLE001 — ошибку отдаём родителю текстом
                response = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                            "traceback": traceback.format_exc()[-800:],
                            "ms": round((time.perf_counter() - started) * 1000.0, 2)}
            try:
                connection.send(response)
            except (BrokenPipeError, OSError):
                return
    finally:
        with __import__("contextlib").suppress(Exception):
            connection.close()


if __name__ == "__main__":  # прямой запуск для отладки: не используется ядром
    raise SystemExit("сателлит запускается только из mcp_hands.probe")
