"""Регрессионные тесты по отчёту `reports/audit_stage1_v6_20261017.md`.

Три требования аудита: сторож модальных окон обязан работать ВО ВРЕМЯ COM-вызова
(иначе он бесполезен), повтор `word_quit` не должен терять ссылку на Word, а процесс,
оставшийся от снятого воркера, обязан добиваться по файлу-реестру.

Отдельно зафиксирован факт, добытый на живом Word: при `DisplayAlerts = 0` открытие
монопольно заблокированного файла даёт НЕ модальное окно, а мгновенную ошибку COM
(«файл используется другим приложением»). Поэтому предложенный в отчёте тест через
блокировку файла проверяет не диалог, а быстрый и понятный отказ.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mcp_hands.events import EventPump
from mcp_hands.office import WORD_IMAGE, ModalWatcher, WordSession, timeout_for
from mcp_hands.probe import (DOMAIN_OPERATION_TIMEOUT_S, ProbeAborted, ProbeProcess,
                             ProbeTimeout)

pytestmark = pytest.mark.office


class Dialog:
    """Тестовое окно класса `#32770` — так выглядит модальный диалог Windows."""

    def __init__(self, title: str) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-u", "-m", "mcp_hands.testing.dialog_window", "--title", title],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.hwnd = 0
        self.pid = 0
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
                break
        time.sleep(0.4)

    def close(self) -> None:
        self.process.kill()
        self.process.communicate()


# --- Дефект 1: сторож обязан работать во время операции -------------------------------


def test_modal_watcher_sees_dialog_class():
    """Сторож узнаёт диалог по классу окна `#32770` и по pid процесса.

    Окно создаётся ПОСЛЕ запуска хука: событие появления окна уже прошло бы, если
    поднять его раньше, и сторож честно ничего не увидел бы.
    """
    with EventPump() as pump:
        watcher = ModalWatcher(pump)
        dialog = Dialog("Файл используется другим приложением?")
        try:
            deadline = time.time() + 5
            while time.time() < deadline and not watcher.recent(dialog.pid):
                time.sleep(0.1)
            detected = watcher.recent(dialog.pid)
            assert detected, "модальное окно класса #32770 не обнаружено"
            assert "#32770" in detected, f"в описании нет класса окна: {detected}"
            assert "?" in detected, f"в описании нет заголовка диалога: {detected}"
        finally:
            dialog.close()


def test_modal_watcher_ignores_other_process():
    """Окно диалога чужого процесса не должно приниматься за модалку Word."""
    dialog = Dialog("Не наш диалог?")
    try:
        with EventPump() as pump:
            watcher = ModalWatcher(pump)
            watcher.check(word_pid=dialog.pid + 12345)
            time.sleep(0.5)
            watcher.check(word_pid=dialog.pid + 12345)
            assert watcher.recent(dialog.pid + 12345) == "", \
                f"сторож отреагировал на чужой процесс: {watcher.seen}"
    finally:
        dialog.close()


def test_call_is_aborted_by_tick_instead_of_waiting_budget():
    """Операция прерывается наблюдателем, а не ожидает полный бюджет.

    Это и есть исправление главного дефекта: раньше сторож опрашивался до вызова,
    а сам вызов блокировал поток на весь бюджет — модалка обнаруживалась бы только
    по его истечении и без имени диалога.
    """
    with ProbeProcess(default_timeout_s=DOMAIN_OPERATION_TIMEOUT_S) as satellite:
        started = time.perf_counter()
        with pytest.raises(ProbeAborted) as error:
            satellite.call("sleep", seconds=10.0, timeout_s=30.0,
                           on_tick=lambda: "модальный диалог Word (#32770): нужен человек")
        elapsed = time.perf_counter() - started
    assert elapsed < 3.0, f"вызов не прерван наблюдателем: {elapsed:.1f} с"
    assert "модальный диалог" in str(error.value)
    assert not satellite.alive, "при прерывании сателлит обязан быть снят"


def test_word_session_reports_modal_and_stops_fast(monkeypatch):
    """WordSession при модальном окне возвращает причину и не ждёт бюджет."""
    with ProbeProcess(default_timeout_s=DOMAIN_OPERATION_TIMEOUT_S) as satellite:
        word = WordSession(satellite)

        class StubWatcher:
            def recent(self, word_pid: int = 0) -> str:
                return "#32770: Файл используется другим приложением?"

        word.modal_watcher = StubWatcher()
        started = time.perf_counter()
        outcome = word._call("sleep", timeout_s=30.0, seconds=10.0)
        elapsed = time.perf_counter() - started
    assert not outcome.ok
    assert elapsed < 3.0, f"операция ждала {elapsed:.1f} с вместо прерывания"
    assert "#32770" in outcome.modal_dialog
    assert "нужен человек" in outcome.reason
    assert outcome.details["budget_s"] == 30.0


def test_locked_file_fails_fast_without_dialog_office_word():
    """Монопольно заблокированный файл: мгновенный понятный отказ, а не диалог.

    `DisplayAlerts = 0` (требование того же аудита) превращает «файл занят» в ошибку
    COM с внятным текстом сразу, без модального окна и без ожидания бюджета.
    """
    workdir = Path(__file__).resolve().parent / ".office_locked"
    workdir.mkdir(exist_ok=True)
    target = workdir / "заблокированный.docx"

    with ProbeProcess(default_timeout_s=DOMAIN_OPERATION_TIMEOUT_S) as satellite:
        word = WordSession(satellite)
        if not word.start().ok:
            pytest.skip("Word недоступен")
        try:
            assert word.new_document("Проверка\n").ok
            assert word.save_as(str(target)).ok
            assert satellite.call("word_close_document", timeout_s=30.0)["documents_left"] == 0
        except Exception as exc:  # noqa: BLE001
            word.quit()
            pytest.skip(f"подготовка не удалась: {exc}")

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                         ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                         wintypes.HANDLE]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        handle = kernel32.CreateFileW(str(target), 0x80000000, 0, None, 3, 0x80, None)
        try:
            assert handle != wintypes.HANDLE(-1).value, "не удалось заблокировать файл"
            started = time.perf_counter()
            outcome = word.open(str(target))
            elapsed = time.perf_counter() - started
        finally:
            kernel32.CloseHandle(handle)
            word.quit()

    assert not outcome.ok, f"открытие заблокированного файла удалось: {outcome.describe()}"
    assert elapsed < 10.0, f"отказ занял {elapsed:.1f} с — Word ждал вместо быстрой ошибки"
    assert "используется другим приложением" in outcome.reason.lower(), \
        f"нет понятной причины отказа: {outcome.reason[:200]}"
    assert outcome.modal_dialog == "", "диалога быть не должно: DisplayAlerts выключены"


# --- Дефект 2: повтор Quit не теряет ссылку ------------------------------------------


def test_quit_retry_does_not_lose_word():
    """Повтор закрытия работает с тем же COM-объектом, а не отчитывается фиктивно.

    Раньше ссылка обнулялась в `finally` даже при ошибке `Quit`, и повтор возвращал
    «уже закрыт», оставляя процесс Word висеть.
    """
    with ProbeProcess(default_timeout_s=DOMAIN_OPERATION_TIMEOUT_S) as satellite:
        word = WordSession(satellite)
        if not word.start().ok:
            pytest.skip("Word недоступен")
        pid = word.pid
        try:
            first = satellite.call("word_quit", save=False, timeout_s=30.0)
            assert first["closed"], first
            second = satellite.call("word_quit", save=False, timeout_s=30.0)
            assert second.get("already") is True, "повтор не распознан как повтор"
            status = satellite.call("word_status", timeout_s=timeout_for("word_status"))
            assert status["running"] is False, f"Word считает себя запущенным: {status}"
        finally:
            word.quit()
    deadline = time.time() + 20
    while time.time() < deadline and ProbeProcess.process_identity(pid)["image"]:
        time.sleep(0.25)
    assert not ProbeProcess.process_identity(pid)["image"], f"процесс {pid} остался висеть"


# --- Дефект 3 и 4: реестр и пути ------------------------------------------------------


def test_registry_keeps_creation_time():
    """Время создания процесса переносится из реестра: иначе защита от чужого PID гаснет."""
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        time.sleep(0.2)
        identity = ProbeProcess.process_identity(victim.pid)
        with ProbeProcess(default_timeout_s=3.0) as satellite:
            satellite.call("register_process", pid=victim.pid, image="python.exe")
            second = ProbeProcess(default_timeout_s=3.0, pid_file=satellite.pid_file)
            added = second.sync_from_registry()
            assert added, "запись из реестра не подобрана"
            tracked = [item for item in second.tracked_processes() if item["pid"] == victim.pid]
            assert tracked and tracked[0]["created"] == identity["created"], \
                f"время создания потеряно: {tracked}"
    finally:
        victim.kill()


def test_open_uses_absolute_paths(target=None):
    """Относительный путь превращается в абсолютный: Word ищет файлы по-своему."""
    from mcp_hands.office import _absolute

    relative = Path("spike") / "diag_word.py"
    absolute = _absolute(str(relative))
    assert Path(absolute).is_absolute()
    assert Path(absolute).exists()
    assert "spike" in absolute


# --- Требование: снятый воркер — Word добивается по реестру ---------------------------


def test_killed_worker_leaves_word_under_supervision():
    """Воркер снят по таймауту — Word обязан умереть по записи из файла-реестра.

    Word поднимает служба DCOM: ребёнком воркера он не является, и без файла-реестра
    такой процесс остаётся висеть с занятыми файлами.
    """
    satellite = ProbeProcess(default_timeout_s=DOMAIN_OPERATION_TIMEOUT_S)
    satellite.start()
    word = WordSession(satellite)
    if not word.start().ok:
        satellite.stop()
        pytest.skip("Word недоступен")
    pid = word.pid
    registry = satellite.pid_file
    try:
        assert pid and ProbeProcess.process_identity(pid)["image"].lower() == WORD_IMAGE.lower()
        entries = json.loads(registry.read_text(encoding="utf-8"))
        assert any(entry["pid"] == pid for entry in entries), \
            f"pid Word не записан в реестр: {entries}"

        satellite.kill()  # имитация снятия по таймауту: воркер завис внутри COM

        successor = ProbeProcess(default_timeout_s=DOMAIN_OPERATION_TIMEOUT_S, pid_file=registry)
        successor.start()
        try:
            added = successor.sync_from_registry()
            assert any(entry["pid"] == pid for entry in added), \
                "наследник не подобрал запись из реестра"
            successor.kill()
        finally:
            successor.stop()

        deadline = time.time() + 20
        while time.time() < deadline and ProbeProcess.process_identity(pid)["image"]:
            time.sleep(0.25)
        assert not ProbeProcess.process_identity(pid)["image"], \
            f"Word {pid} пережил снятие воркера — утечка процесса"
    finally:
        if ProbeProcess.process_identity(pid)["image"]:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        satellite.stop()
        registry.unlink(missing_ok=True)
