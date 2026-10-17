"""Общие приспособления тестов: запуск мишеней и координатное пространство.

Тесты ядра работают на живом стенде (Windows), поэтому в них есть две категории:
быстрые проверки арифметики и «живые» проверки, которым нужны окна. Мишени поднимаются
как отдельные процессы через `mcp_hands.testing`.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_hands import enable_dpi_awareness  # noqa: E402

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Target:
    """Запущенная мишень: дескриптор окна, процесс и журнал попаданий."""

    def __init__(self, module: str, title: str, extra: list[str] | None = None) -> None:
        self.title = title
        self.log_path = Path(__file__).resolve().parent / f".{title.split()[-1]}.log"
        self.log_path.write_text("", encoding="utf-8")
        command = [sys.executable, "-u", "-m", module, "--title", title,
                   "--click-log", str(self.log_path), *(extra or [])]
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding="utf-8", errors="replace",
                                        creationflags=CREATE_NO_WINDOW)
        self.hwnd = 0
        self.pid = 0
        self.desktop = ""
        self.ready = False
        deadline = time.time() + 15
        while time.time() < deadline:
            line = self.process.stdout.readline()
            if not line:
                break
            if line.startswith("HWND="):
                self.hwnd = int(line.strip().split("=", 1)[1], 16)
            elif line.startswith("PID="):
                self.pid = int(line.strip().split("=", 1)[1])
            elif line.startswith("DESKTOP="):
                self.desktop = line.strip().split("=", 1)[1]
            # Ждём и строку с рабочим столом: мишень печатает её после PID,
            # и без неё проверка «окно на столе ввода» осталась бы без данных.
            if self.hwnd and self.pid and self.desktop:
                self.ready = True
                break
        time.sleep(0.6)  # окну нужно дорисоваться и попасть в дерево UIA
        self.bring_to_front()

    def bring_to_front(self) -> bool:
        """Поднимает мишень: иначе её перекрывает постороннее окно и клик уходит не туда."""
        from mcp_hands.geometry import CoordinateSpace
        from mcp_hands.input import Actuator

        return bool(Actuator(CoordinateSpace()).activate(self.hwnd).get("ok"))

    def clicks(self) -> list[str]:
        return [line for line in self.log_path.read_text(encoding="utf-8").splitlines()
                if line.startswith("CLICK")]

    def events(self) -> list[str]:
        return self.log_path.read_text(encoding="utf-8").splitlines()

    def close(self) -> None:
        self.process.kill()
        self.process.communicate()
        self.log_path.unlink(missing_ok=True)


@pytest.fixture(scope="session", autouse=True)
def dpi_awareness():
    """Процесс тестов обязан быть per-monitor aware, иначе координаты виртуализуются."""
    mode = enable_dpi_awareness()
    return mode


@pytest.fixture(scope="session")
def capture_available() -> bool:
    """Доступен ли захват экрана в этой сессии.

    В ограниченных сессиях (служба, заблокированный рабочий стол) отказывают и DXGI,
    и BitBlt — тесты, которым нужен кадр, должны пропускаться с внятной причиной,
    а не падать, создавая ложную картину поломки кода.
    """
    from mcp_hands.screen import ScreenCaptureError, ScreenGrabber

    try:
        ScreenGrabber().grab(timeout_s=0.2)
        return True
    except ScreenCaptureError:
        return False


@pytest.fixture
def require_capture(capture_available: bool) -> None:
    if not capture_available:
        pytest.skip("захват экрана недоступен в этой сессии: DXGI и BitBlt отказали")


def input_desktop_name() -> str:
    """Имя рабочего стола, на который уходит ввод в этом процессе."""
    from mcp_hands.geometry import CoordinateSpace
    from mcp_hands.input import Actuator

    return Actuator(CoordinateSpace()).own_desktop()


@pytest.fixture
def require_input_desktop(target):
    """Пропускает тест ввода, если окно мишени на другом рабочем столе.

    В песочнице или служебном сеансе окно мишени может оказаться на параллельном столе:
    тогда клики физически не доходят, и это отказ среды, а не поломка кода. Мишень
    старается создаться на столе ввода сама; если не удалось — тест честно пропускается
    с указанием обоих имён.
    """
    ours = input_desktop_name()
    if target.desktop and ours and target.desktop != ours:
        pytest.skip(f"мишень на рабочем столе {target.desktop!r}, ввод идёт на {ours!r}")


@pytest.fixture
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
        fixture.close()


@pytest.fixture
def hang_target():
    """Окно, перестающее разбирать сообщения через 0.4 с после старта."""
    fixture = Target("mcp_hands.testing.hang_window", "MCP-Hands pytest hang",
                     extra=["--responsive-sec", "0.4", "--freeze-sec", "60"])
    try:
        yield fixture
    finally:
        fixture.close()


@pytest.fixture(scope="session", autouse=True)
def office_janitor():
    """Не даёт тестам оставить за собой процессы Office.

    COM-сервер Word поднимает служба DCOM: он не ребёнок тестового процесса, и обычная
    уборка его не задевает. Процессы, появившиеся за время прогона, снимаются в конце —
    иначе следующий запуск получит висящий WINWORD.EXE с занятыми файлами.
    """
    import subprocess
    import sys
    import time

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from mcp_hands.probe_worker import Worker

    def winword_pids() -> set:
        try:
            return Worker._winword_pids()
        except Exception:  # noqa: BLE001 — уборка не должна ломать прогон
            return set()

    before = winword_pids()
    yield
    leaked = winword_pids() - before
    for pid in leaked:
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=10)
        except Exception:  # noqa: BLE001
            pass
    if leaked:
        time.sleep(0.5)
        remaining = winword_pids() - before
        assert not remaining, f"после прогона остались процессы Word: {sorted(remaining)}"
