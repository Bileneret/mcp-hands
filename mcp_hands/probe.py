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

    def __init__(self, default_timeout_s: float = 1.0, name: str = "mcp-hands-probe") -> None:
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
        self._process = multiprocessing.Process(target=probe_worker.main, args=(child,),
                                                name=self.name, daemon=True)
        self._process.start()
        child.close()

    def stop(self) -> None:
        self.kill_tracked()
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

    def track_process(self, pid: int, note: str = "") -> None:
        """Берёт процесс под опеку: он будет убит вместе с сателлитом.

        Нужно для COM-серверов Office: их запускает служба DCOM, ребёнком воркера они
        не являются, и при снятии сателлита остаются висеть, блокируя документы.
        """
        if pid and pid > 0:
            self._tracked[pid] = note

    def untrack_process(self, pid: int) -> None:
        self._tracked.pop(pid, None)

    def tracked_processes(self) -> list[dict]:
        return [{"pid": pid, "note": note} for pid, note in self._tracked.items()]

    def kill_tracked(self) -> list[int]:
        """Принудительно завершает опекаемые процессы и возвращает список убитых."""
        import subprocess

        killed: list[int] = []
        for pid, _note in list(self._tracked.items()):
            try:
                # text=False: taskkill отвечает в кодировке консоли, и декодирование
                # как UTF-8 роняет поток чтения ещё до разбора результата.
                completed = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                           capture_output=True, timeout=10)
                if completed.returncode == 0:
                    killed.append(pid)
            except Exception:  # noqa: BLE001 — отсутствующий процесс это не ошибка
                pass
            self._tracked.pop(pid, None)
        return killed

    def kill(self) -> None:
        """Аварийное снятие: единственный способ разблокировать зависший вызов UIA."""
        tracked_killed = self.kill_tracked()
        if tracked_killed:
            self.stats.history.append({"op": "kill_tracked", "killed": tracked_killed})
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

    def ping(self) -> dict:
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
        return self.call("hotkey", keys=list(keys))
