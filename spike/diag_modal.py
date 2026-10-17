"""Диагностика: что делает Word при открытии файла, заблокированного монопольно.

Аудит v6 предлагает такой тест для сторожа модалок. Сначала смотрим факты: появляется
ли реальное модальное окно, за сколько секунд это видно и что вернёт операция.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_hands.events import EventPump
from mcp_hands.office import ModalWatcher, WordSession, timeout_for
from mcp_hands.probe import DOMAIN_OPERATION_TIMEOUT_S, ProbeProcess


def lock_exclusive(path: Path):
    """Открывает файл без разделения доступа: Word получит «файл занят»."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)  # GENERIC_READ, OPEN_EXISTING
    invalid = wintypes.HANDLE(-1).value
    return (None, ctypes.get_last_error()) if handle == invalid else (handle, 0)


def main() -> int:
    workdir = Path(__file__).resolve().parent / ".office_diag"
    workdir.mkdir(exist_ok=True)
    target = workdir / "заблокированный.docx"

    with ProbeProcess(default_timeout_s=DOMAIN_OPERATION_TIMEOUT_S) as satellite:
        word = WordSession(satellite)
        print("старт:", word.start().describe()["ok"], "| pid:", word.pid)
        created = word.new_document("Проверка блокировки\n")
        print("создан документ:", created.ok)
        saved = word.save_as(str(target))
        print("сохранён:", saved.ok, saved.value if saved.ok else saved.reason)
        # закрываем документ, чтобы Word сам его не держал
        print("закрытие документа:", satellite.call("word_close_document", timeout_s=30.0))

        handle, error = lock_exclusive(target)
        print(f"файл заблокирован монопольно: handle={handle}, ошибка={error}")

        with EventPump() as pump:
            watcher = ModalWatcher(pump)
            word.modal_watcher = watcher
            word.probe.call  # noqa: B018 — проверяем доступность API
            budget = 25.0
            started = time.perf_counter()
            outcome = word._call("word_open", timeout_s=budget, path=str(target))
            elapsed = time.perf_counter() - started
            print(f"открытие заблокированного файла: {elapsed:.1f} с из бюджета {budget} с")
            print("  ok:", outcome.ok, "| reason:", outcome.reason[:160])
            print("  modal_dialog:", outcome.modal_dialog)
            print("  сателлит жив:", satellite.alive)
            print("  события сторожa:", watcher.seen[-3:])

        if handle:
            ctypes.WinDLL("kernel32").CloseHandle(handle)
        print("закрытие Word:", word.quit().describe()["ok"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
