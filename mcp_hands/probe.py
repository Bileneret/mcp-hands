"""Процесс-сателлит: UIA, COM и физический ввод живут в отдельном процессе.

Зачем так: замерено 300–800 мс на запуск Python и генерацию COM-обёрток `comtypes`,
поэтому запускать процесс на каждый шаг неприемлемо — сателлит живёт постоянно.
Но зависший провайдер UIA блокирует вызывающий поток навсегда (без таймаута вызов
не возвращается и за 45 с), поэтому у каждого вызова есть бюджет: не уложился —
процесс убивается и поднимается заново, а шаг агента получает явный отказ вместо
зависания.

Наружу — один метод `call(op, timeout_s=..., **kwargs)`, внутри — канал, таймаут
и перезапуск.
"""

from __future__ import annotations

import multiprocessing
import time
from dataclasses import dataclass, field


# Бюджет вызова для прикладных операций. Он обязан быть БОЛЬШЕ самого долгого
# внутреннего ожидания воркера: активация окна перебирает приёмы передачи фокуса до
# ACTIVATION_TIMEOUT_S секунд, поэтому бюджет в 1 с снимал бы сателлит прямо посреди
# штатной работы (найдено аудитом v3: на перекрытом окне клик занимает 1.65-1.8 с).
DOMAIN_OPERATION_TIMEOUT_S = 6.0


@dataclass
class ProbeStats:
    """Учёт вызовов: без него отказ сателлита выглядит как «просто медленно»."""

    calls: int = 0
    timeouts: int = 0
    restarts: int = 0
    errors: int = 0
    total_ms: float = 0.0
    history: list[dict] = field(default_factory=list)

    def record(self, op: str, ms: float, outcome: str) -> None:
        self.calls += 1
        self.total_ms += ms
        self.history.append({"op": op, "ms": round(ms, 2), "outcome": outcome})
        del self.history[:-20]

    def describe(self) -> dict:
        return {"calls": self.calls, "timeouts": self.timeouts, "restarts": self.restarts,
                "errors": self.errors,
                "avg_ms": round(self.total_ms / self.calls, 2) if self.calls else None,
                "history": self.history[-5:]}


class ProbeTimeout(RuntimeError):
    """Сателлит не уложился в бюджет: процесс убит, следующий вызов поднимет новый."""


class ProbeError(RuntimeError):
    """Операция завершилась ошибкой внутри сателлита (не таймаут)."""


class ProbeProcess:
    """Постоянный процесс-исполнитель с бюджетом на вызов."""

    def __init__(self, default_timeout_s: float = 1.0, name: str = "mcp-hands-probe",
                 pid_file: "Path | None" = None) -> None:
        import os
        import tempfile
        from pathlib import Path as _Path

        # Имя файла-реестра обязано быть уникальным для экземпляра: два супервизора
        # в одном каталоге временных файлов иначе делят один реестр, затирают записи
        # друг друга и при снятии убивают чужие процессы.
        self.pid_file = _Path(pid_file) if pid_file else (
            _Path(tempfile.gettempdir()) / f"{name}_{os.getpid()}_{id(self)}.pids.json")
        self.pid_file.unlink(missing_ok=True)
        self.default_timeout_s = default_timeout_s
        self.name = name
        self.stats = ProbeStats()
        self._process: multiprocessing.Process | None = None
        self._connection = None
        # Процессы, запущенные не как дети воркера: COM-серверы Office поднимает служба
        # DCOM, поэтому убийство воркера их не задевает. Их надо отслеживать отдельно,
        # иначе после каждого снятия остаётся висящий WINWORD.EXE с занятым файлом.
        self._tracked: dict[int, str] = {}

    # --- жизненный цикл ---------------------------------------------------

    def start(self) -> None:
        if self._process is not None and self._process.is_alive():
            return
        from . import probe_worker

        parent, child = multiprocessing.Pipe()
        self._connection = parent
        self._process = multiprocessing.Process(target=probe_worker.main,
                                                args=(child, str(self.pid_file)),
                                                name=self.name, daemon=True)
        self._process.start()
        child.close()

    def stop(self) -> None:
        self.sync_from_registry()
        self.kill_tracked()
        self.pid_file.unlink(missing_ok=True)
        if self._connection is not None:
            try:
                self._connection.send(None)  # вежливая просьба завершиться
            except (BrokenPipeError, OSError):
                pass
            try:
                self._connection.close()
            except OSError:
                pass
            self._connection = None
        if self._process is not None:
            self._process.join(1.0)
            if self._process.is_alive():
                self._process.kill()
                self._process.join(1.0)
            self._process = None

    # --- отслеживаемые процессы ------------------------------------------

    def track_process(self, pid: int, note: str = "", image: str = "") -> None:
        """Берёт процесс под опеку: он будет убит вместе с сателлитом.

        Нужно для COM-серверов Office: их запускает служба DCOM, ребёнком воркера они
        не являются, и при снятии сателлита остаются висеть, блокируя документы.

        `image` — ожидаемое имя исполняемого файла (например, `WINWORD.EXE`). Перед
        убийством образ проверяется: Windows переиспользует идентификаторы процессов,
        и убивать по одному лишь номеру опасно.
        """
        if pid and pid > 0:
            identity = self.process_identity(pid)
            self._tracked[pid] = {"note": note, "image": image or identity["image"],
                                  "created": identity["created"]}

    def untrack_process(self, pid: int) -> None:
        self._tracked.pop(pid, None)

    def tracked_processes(self) -> list[dict]:
        return [{"pid": pid, **info} for pid, info in self._tracked.items()]

    @staticmethod
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
            image = buffer.value.rsplit("\\", 1)[-1] if \
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
    def process_image_name(pid: int) -> str:
        """Имя исполняемого файла процесса: защита от убийства по переиспользованному PID."""
        import ctypes
        import ctypes.wintypes as wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                        wintypes.LPWSTR,
                                                        ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buffer))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return buffer.value.rsplit("\\", 1)[-1]
        finally:
            kernel32.CloseHandle(handle)
        return ""

    def kill_tracked(self) -> list[dict]:
        """Завершает опекаемые процессы; возвращает отчёт по каждому.

        Перед убийством проверяется имя образа: если оно не совпадает с записанным,
        процесс не наш — номер успел переиспользоваться, и трогать его нельзя.
        """
        import subprocess

        report: list[dict] = []
        for pid, info in list(self._tracked.items()):
            image = info.get("image", "")
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
                               "reason": f"образ не совпал с ожидаемым {image}: PID переиспользован"})
            else:
                try:
                    # text=False: taskkill отвечает в кодировке консоли, и декодирование
                    # как UTF-8 роняет поток чтения ещё до разбора результата.
                    completed = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                               capture_output=True, timeout=10)
                    report.append({"pid": pid, "killed": completed.returncode == 0,
                                   "image": actual})
                except Exception as exc:  # noqa: BLE001
                    report.append({"pid": pid, "killed": False, "reason": str(exc)})
            self._tracked.pop(pid, None)
        return report

    # --- реестр процессов, созданных сателлитом ---------------------------

    def register_external(self, pid: int, image: str = "", note: str = "") -> None:
        """Регистрирует процесс, о котором сателлит сообщил через файл-реестр.

        Нужно потому, что COM-сервер (Word) создаётся ВНУТРИ воркера. Если воркер
        зависнет в блокирующем COM-вызове, он не успеет сообщить pid ответом на запрос,
        и Word утечёт. Поэтому воркер пишет pid в файл сразу после запуска, а супервизор
        читает файл в момент аварийного снятия — независимо от состояния воркера.
        """
        if pid and pid > 0 and pid not in self._tracked:
            self._tracked[pid] = {"note": note or "из реестра сателлита", "image": image}

    def sync_from_registry(self) -> list[dict]:
        """Подбирает процессы, записанные сателлитом в файл-реестр."""
        if not self.pid_file or not self.pid_file.exists():
            return []
        import json

        try:
            entries = json.loads(self.pid_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        added = []
        for entry in entries if isinstance(entries, list) else []:
            pid = int(entry.get("pid", 0))
            if pid and pid not in self._tracked:
                self.register_external(pid, entry.get("image", ""), entry.get("note", ""))
                added.append(entry)
        return added

    def kill(self) -> None:
        """Аварийное снятие: единственный способ разблокировать зависший вызов UIA."""
        self.sync_from_registry()  # подбираем то, о чём сателлит успел записать
        tracked_killed = self.kill_tracked()
        if tracked_killed:
            self.stats.history.append({"op": "kill_tracked", "processes": tracked_killed})
        if self._process is not None:
            self._process.kill()
            self._process.join(1.0)
            self._process = None
        if self._connection is not None:
            try:
                self._connection.close()
            except OSError:
                pass
            self._connection = None
        self.stats.restarts += 1

    def __enter__(self) -> "ProbeProcess":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.is_alive()

    # --- вызов ------------------------------------------------------------

    def call(self, op: str, timeout_s: float | None = None, **kwargs) -> object:
        """Выполняет операцию в сателлите. Таймаут — убийство процесса, а не ожидание."""
        budget = timeout_s if timeout_s is not None else self.default_timeout_s
        if not self.alive:
            self.start()
        started = time.perf_counter()
        try:
            self._connection.send({"op": op, "kwargs": kwargs})
        except (BrokenPipeError, OSError, AttributeError) as exc:
            self.stats.errors += 1
            self.kill()
            raise ProbeError(f"канал сателлита недоступен: {exc}") from exc

        if not self._connection.poll(budget):
            elapsed = (time.perf_counter() - started) * 1000.0
            self.stats.timeouts += 1
            self.stats.record(op, elapsed, "timeout")
            self.kill()
            raise ProbeTimeout(
                f"операция {op!r} не ответила за {budget:.2f} с — сателлит снят и будет поднят заново")

        try:
            response = self._connection.recv()
        except (EOFError, OSError) as exc:
            self.stats.errors += 1
            self.kill()
            raise ProbeError(f"сателлит оборвал канал: {exc}") from exc

        elapsed = (time.perf_counter() - started) * 1000.0
        if not response.get("ok"):
            self.stats.errors += 1
            self.stats.record(op, elapsed, "error")
            raise ProbeError(response.get("error", "неизвестная ошибка сателлита"))
        self.stats.record(op, elapsed, "ok")
        return response.get("result")

    # --- удобные обёртки --------------------------------------------------

    def _budget(self, timeout_s: float | None) -> float:
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
        return self.call("hotkey", keys=list(keys), timeout_s=self._budget(None))
