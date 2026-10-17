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
            if self.hwnd and self.pid:
                self.ready = True
                break
        time.sleep(0.6)  # окну нужно дорисоваться и попасть в дерево UIA

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


@pytest.fixture
def target():
    """Окно-мишень с кнопками alpha, beta, gamma."""
    fixture = Target("mcp_hands.testing.fixture_window", "MCP-Hands pytest target")
    try:
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
