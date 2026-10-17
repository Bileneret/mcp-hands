"""Регрессионные тесты по отчёту `reports/audit_stage1_v3_20261017.md`.

Проверяются: согласованность бюджета сателлита со временем активации окна,
уникальность файла-реестра процессов, снятие процесса с учёта и опознание процесса
по имени образа вместе со временем создания.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mcp_hands.input import ACTIVATION_TIMEOUT_S
from mcp_hands.probe import DOMAIN_OPERATION_TIMEOUT_S, ProbeProcess


def test_operation_budget_exceeds_activation_time():
    """Бюджет сателлита обязан быть больше самого долгого внутреннего ожидания.

    Дефект, найденный аудитом v3: клик получал дефолтный бюджет 1 с, а внутри
    активация окна перебирает приёмы передачи фокуса до 1.5 с — супервизор убивал
    сателлит прямо посреди штатной работы. На перекрытом окне это воспроизводится
    стабильно (в отчёте замерено 1.65–1.8 с на операцию).
    """
    assert DOMAIN_OPERATION_TIMEOUT_S > ACTIVATION_TIMEOUT_S + 1.0, \
        (f"бюджет операций {DOMAIN_OPERATION_TIMEOUT_S} с не покрывает активацию "
         f"{ACTIVATION_TIMEOUT_S} с — сателлит будет сниматься на ровном месте")


def test_domain_wrappers_do_not_use_short_default():
    """Обёртки прикладных операций не наследуют короткий дефолт `call`."""
    probe = ProbeProcess(default_timeout_s=0.5)
    assert probe._budget(None) == DOMAIN_OPERATION_TIMEOUT_S
    assert probe._budget(2.0) == 2.0, "явно заданный бюджет обязан уважаться"


def test_registry_files_are_unique_per_instance():
    """Два супервизора не должны делить один файл-реестр процессов.

    Иначе второй экземпляр удаляет файл первого, воркеры затирают записи друг друга,
    и при аварийном снятии убиваются чужие процессы.
    """
    with ProbeProcess(default_timeout_s=2.0) as first, ProbeProcess(default_timeout_s=2.0) as second:
        assert first.pid_file != second.pid_file, "файлы-реестры совпали"
        assert str(__import__("os").getpid()) in first.pid_file.name, \
            "в имени нет идентификатора процесса супервизора"


def test_unregister_removes_process_from_registry():
    """Штатное завершение обязано снимать запись, иначе остаётся мёртвый PID."""
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        time.sleep(0.2)
        with ProbeProcess(default_timeout_s=3.0) as satellite:
            satellite.call("register_process", pid=victim.pid, image="python.exe")
            entries = json.loads(satellite.pid_file.read_text(encoding="utf-8"))
            assert entries and entries[0]["pid"] == victim.pid
            assert entries[0]["created"], "время создания процесса не записано"

            answer = satellite.call("unregister_process", pid=victim.pid)
            assert answer["ok"] and answer["removed"] == 1
            entries = json.loads(satellite.pid_file.read_text(encoding="utf-8"))
            assert entries == [], f"запись осталась: {entries}"

            # Процесс не опекается — снятие сателлита его не тронет.
            satellite.kill()
        time.sleep(0.3)
        assert victim.poll() is None, "снятый с учёта процесс убит напрасно"
    finally:
        victim.kill()


def test_kill_refuses_when_creation_time_differs():
    """Время создания делает опознание процесса однозначным.

    Одно имя образа не спасает: «python.exe» на машине не один, и переиспользованный
    номер может указывать на чужой процесс с тем же именем.
    """
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        time.sleep(0.2)
        with ProbeProcess(default_timeout_s=2.0) as satellite:
            identity = ProbeProcess.process_identity(victim.pid)
            assert identity["created"], "время создания не получено"
            satellite.track_process(victim.pid, note="подмена времени")
            satellite._tracked[victim.pid]["created"] = identity["created"] + 1  # чужой процесс
            report = satellite.kill_tracked()
        assert report and not report[0]["killed"]
        assert "время создания не совпало" in report[0]["reason"]
        time.sleep(0.3)
        assert victim.poll() is None, "процесс убит несмотря на несовпадение времени"
    finally:
        victim.kill()


def test_kill_accepts_matching_identity():
    """Совпавшие имя и время создания — процесс наш, он завершается."""
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        time.sleep(0.2)
        with ProbeProcess(default_timeout_s=2.0) as satellite:
            satellite.track_process(victim.pid, note="наш процесс", image="python.exe")
            report = satellite.kill_tracked()
        assert report and report[0]["killed"], report
        deadline = time.time() + 5
        while time.time() < deadline and victim.poll() is None:
            time.sleep(0.1)
        assert victim.poll() is not None
    finally:
        if victim.poll() is None:
            victim.kill()


def test_click_uses_process_id_from_node(target):
    """PID для проверки прав берётся из узла UIA, без обхода всех окон системы.

    Обход окон ради того же числа стоил десятки-сотни миллисекунд на каждый клик
    (замечание аудита v3). Признак работы по узлу — заполненная проверка прав в ответе.
    """
    with ProbeProcess(default_timeout_s=DOMAIN_OPERATION_TIMEOUT_S) as satellite:
        result = satellite.click(target.hwnd, name="beta", control_type="button")
    assert result.get("ok"), f"клик не выполнен: {result}"
    rights = result.get("rights")
    assert rights, "проверка прав не выполнена — PID не получен из узла"
    assert rights["target"]["level"] is not None
    assert rights["target"]["level"] == rights["ours"]["level"], \
        "уровни целостности мишени и агента должны совпадать"


def test_element_node_carries_process_id(target):
    """Свойство обязано быть в узле: на нём держится проверка прав без обхода окон."""
    with ProbeProcess(default_timeout_s=DOMAIN_OPERATION_TIMEOUT_S) as satellite:
        node = satellite.element(target.hwnd, name="alpha", control_type="button")
    assert node is not None
    assert node.get("process_id") == target.pid, \
        f"в узле {node.get('process_id')}, у мишени {target.pid}"
