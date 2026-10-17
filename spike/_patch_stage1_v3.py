"""Патч по итогам аудита v3: бюджет операций, уникальность реестра, опознание процесса."""

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


# ---------------- probe.py ----------------
patch("mcp_hands/probe.py", [
    ('''@dataclass
class ProbeStats:''',
     '''# Бюджет вызова для прикладных операций. Он обязан быть БОЛЬШЕ самого долгого
# внутреннего ожидания воркера: активация окна перебирает приёмы передачи фокуса до
# ACTIVATION_TIMEOUT_S секунд, поэтому бюджет в 1 с снимал бы сателлит прямо посреди
# штатной работы (найдено аудитом v3: на перекрытом окне клик занимает 1.65-1.8 с).
DOMAIN_OPERATION_TIMEOUT_S = 6.0


@dataclass
class ProbeStats:'''),
    ('''    def __init__(self, default_timeout_s: float = 1.0, name: str = "mcp-hands-probe",
                 pid_file: "Path | None" = None) -> None:
        import tempfile
        from pathlib import Path as _Path

        self.pid_file = _Path(pid_file) if pid_file else \\
            _Path(tempfile.gettempdir()) / f"{name}.pids.json"''',
     '''    def __init__(self, default_timeout_s: float = 1.0, name: str = "mcp-hands-probe",
                 pid_file: "Path | None" = None) -> None:
        import os
        import tempfile
        from pathlib import Path as _Path

        # Имя файла-реестра обязано быть уникальным для экземпляра: два супервизора
        # в одном каталоге временных файлов иначе делят один реестр, затирают записи
        # друг друга и при снятии убивают чужие процессы.
        self.pid_file = _Path(pid_file) if pid_file else (
            _Path(tempfile.gettempdir()) / f"{name}_{os.getpid()}_{id(self)}.pids.json")'''),
    ('''    @staticmethod
    def process_image_name(pid: int) -> str:''',
     '''    @staticmethod
    def process_identity(pid: int) -> dict:
        """Имя образа и время создания процесса.

        Одного имени мало: Windows переиспользует идентификаторы, и «тот же PID» может
        оказаться совсем другим процессом с тем же именем. Время создания (FILETIME)
        делает проверку однозначной.
        """
        import ctypes
        import ctypes.wintypes as wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                        wintypes.LPWSTR,
                                                        ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                             ctypes.c_void_p, ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return {"image": "", "created": None}
        try:
            buffer = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buffer))
            image = buffer.value.rsplit("\\\\", 1)[-1] if \\
                kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)) else ""
            created = ctypes.c_ulonglong(0)
            exited = ctypes.c_ulonglong(0)
            kernel = ctypes.c_ulonglong(0)
            user = ctypes.c_ulonglong(0)
            ok = kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                          ctypes.byref(kernel), ctypes.byref(user))
            return {"image": image, "created": int(created.value) if ok else None}
        finally:
            kernel32.CloseHandle(handle)

    @staticmethod
    def process_image_name(pid: int) -> str:'''),
    ('''        if pid and pid > 0:
            self._tracked[pid] = {"note": note, "image": image}''',
     '''        if pid and pid > 0:
            identity = self.process_identity(pid)
            self._tracked[pid] = {"note": note, "image": image or identity["image"],
                                  "created": identity["created"]}'''),
    ('''            image = info.get("image", "")
            actual = self.process_image_name(pid)
            if not actual:
                report.append({"pid": pid, "killed": False, "reason": "процесса уже нет"})
            elif image and actual.lower() != image.lower():
                report.append({"pid": pid, "killed": False, "image": actual,
                               "reason": f"образ не совпал с ожидаемым {image}: PID переиспользован"})''',
     '''            image = info.get("image", "")
            created = info.get("created")
            identity = self.process_identity(pid)
            actual = identity["image"]
            if not actual:
                report.append({"pid": pid, "killed": False, "reason": "процесса уже нет"})
            elif created is not None and identity["created"] != created:
                report.append({"pid": pid, "killed": False, "image": actual,
                               "reason": "время создания не совпало: PID переиспользован"})
            elif image and actual.lower() != image.lower():
                report.append({"pid": pid, "killed": False, "image": actual,
                               "reason": f"образ не совпал с ожидаемым {image}: PID переиспользован"})'''),
    ('''    def ping(self) -> dict:
        return self.call("ping", timeout_s=5.0)

    def windows(self, **kwargs) -> list[dict]:
        return self.call("windows", **kwargs)

    def element(self, hwnd: int, **kwargs) -> dict | None:
        return self.call("element", hwnd=hwnd, **kwargs)

    def walk(self, hwnd: int, **kwargs) -> dict:
        return self.call("walk", hwnd=hwnd, **kwargs)

    def click(self, hwnd: int, **kwargs) -> dict:
        return self.call("click", hwnd=hwnd, **kwargs)

    def type_text(self, text: str) -> dict:
        return self.call("type", text=text)

    def hotkey(self, *keys: int) -> dict:
        return self.call("hotkey", keys=list(keys))''',
     '''    def _budget(self, timeout_s: float | None) -> float:
        """Прикладная операция всегда получает бюджет больше дефолтного."""
        return DOMAIN_OPERATION_TIMEOUT_S if timeout_s is None else timeout_s

    def ping(self) -> dict:
        return self.call("ping", timeout_s=5.0)

    def windows(self, timeout_s: float | None = None, **kwargs) -> list[dict]:
        return self.call("windows", timeout_s=self._budget(timeout_s), **kwargs)

    def element(self, hwnd: int, timeout_s: float | None = None, **kwargs) -> dict | None:
        return self.call("element", hwnd=hwnd, timeout_s=self._budget(timeout_s), **kwargs)

    def walk(self, hwnd: int, timeout_s: float | None = None, **kwargs) -> dict:
        return self.call("walk", hwnd=hwnd, timeout_s=self._budget(timeout_s), **kwargs)

    def click(self, hwnd: int, timeout_s: float | None = None, **kwargs) -> dict:
        # Бюджет больше времени активации окна: иначе сателлит снимается посреди
        # нормальной передачи фокуса (дефект, найденный аудитом v3).
        return self.call("click", hwnd=hwnd, timeout_s=self._budget(timeout_s), **kwargs)

    def type_text(self, text: str, timeout_s: float | None = None) -> dict:
        return self.call("type", text=text, timeout_s=self._budget(timeout_s))

    def hotkey(self, *keys: int) -> dict:
        return self.call("hotkey", keys=list(keys), timeout_s=self._budget(None))'''),
], "бюджет прикладных операций, уникальный реестр, опознание процесса")

# ---------------- probe_worker.py ----------------
patch("mcp_hands/probe_worker.py", [
    ('''    def op_register_process(self, pid: int, image: str = "", note: str = "") -> dict:''',
     '''    def op_unregister_process(self, pid: int) -> dict:
        """Убирает процесс из файла-реестра при штатном завершении.

        Без этого после корректного `word.Quit()` в реестре остаётся запись, и при
        следующем аварийном снятии супервизор попытается убить уже несуществующий
        (или, хуже, переиспользованный) процесс.
        """
        import json

        if not self.pid_file:
            return {"ok": False, "reason": "файл-реестр не задан"}
        path = __import__("pathlib").Path(self.pid_file)
        if not path.exists():
            return {"ok": True, "removed": 0}
        with __import__("contextlib").suppress(ValueError, OSError):
            entries = json.loads(path.read_text(encoding="utf-8")) or []
        remaining = [entry for entry in entries if int(entry.get("pid", 0)) != pid]
        path.write_text(json.dumps(remaining, ensure_ascii=False), encoding="utf-8")
        return {"ok": True, "removed": len(entries) - len(remaining), "total": len(remaining)}

    def op_register_process(self, pid: int, image: str = "", note: str = "") -> dict:'''),
    ('''        entries = [entry for entry in entries if int(entry.get("pid", 0)) != pid]
        entries.append({"pid": pid, "image": image, "note": note})''',
     '''        from .probe import ProbeProcess

        identity = ProbeProcess.process_identity(pid)
        entries = [entry for entry in entries if int(entry.get("pid", 0)) != pid]
        entries.append({"pid": pid, "image": image or identity["image"],
                        "created": identity["created"], "note": note})'''),
    ('''        window = next((w for w in self.op_windows() if w["hwnd"] == hwnd), None)
        return self.actuator.click_node(node, pid=window["pid"] if window else 0,
                                        window_hwnd=hwnd)''',
     '''        # PID берётся из самого узла UIA: обход всех окон системы ради того же числа
        # стоил десятки-сотни миллисекунд на каждый клик (замечание аудита v3).
        return self.actuator.click_node(node, pid=int(node.get("process_id") or 0),
                                        window_hwnd=hwnd)'''),
], "снятие с учёта, время создания в реестре, PID из узла")

# ключ node: process_id попадает в узел только если он есть в профиле свойств
patch("mcp_hands/uia.py", [
    ('''MINIMAL_PROPERTIES = tuple((name, prop) for name, prop in _BASE_PROPERTIES
                           if name in ("name", "automation_id", "control_type", "rect", "enabled"))''',
     '''MINIMAL_PROPERTIES = tuple((name, prop) for name, prop in _BASE_PROPERTIES
                           if name in ("name", "automation_id", "control_type", "rect", "enabled",
                                       "process_id"))'''),
], "process_id в минимальном профиле свойств")

# ---------------- tests ----------------
patch("tests/test_regressions_audit_v2.py", [
    ('''def test_crop_window_handles_maximized_window(target):''',
     '''def test_crop_window_handles_maximized_window(target, require_capture):'''),
], "фикстура доступности захвата в тесте максимизированного окна")

patch("mcp_hands/input.py", [
    ('''    def activate(self, hwnd: int, timeout_s: float = 1.5) -> dict:''',
     '''    def activate(self, hwnd: int, timeout_s: float = ACTIVATION_TIMEOUT_S) -> dict:'''),
    ('''INPUT_MOUSE = 0
INPUT_KEYBOARD = 1''',
     '''INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

# Сколько времени активация окна перебирает приёмы передачи фокуса. Бюджет операций
# сателлита обязан быть больше этого значения (см. probe.DOMAIN_OPERATION_TIMEOUT_S).
ACTIVATION_TIMEOUT_S = 1.5'''),
], "константа времени активации окна")
