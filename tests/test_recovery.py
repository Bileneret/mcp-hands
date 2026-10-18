"""Аварийные тесты этапа 4 (шаг 3): поведение при сбоях среды.

Проверяем не «счастье», а именно отказы: занятый монопольно файл, оборванный
сателлит (рекавери), честность метрик человека. Все — на изолированных
артефактах; Office-тесты идут под маркером `office` и пропускаются там, где
стенд недоступен.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

import pytest

from mcp_hands.brain.plan import parse_plan
from mcp_hands.brain.runner import RunnerLimits, run_plan
from mcp_hands.memory import Memory
from mcp_hands.probe import DOMAIN_OPERATION_TIMEOUT_S, ProbeProcess


def test_memory_human_metrics_and_migration(tmp_path):
    """Метрика автономности (рекомендация аудита v17): колонки human_*,
    агрегация autonomy.rate; миграция старой базы без колонок работает."""
    db = tmp_path / "память.db"
    import sqlite3

    legacy = sqlite3.connect(db)
    legacy.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, task_id TEXT, status TEXT, ok INT,"
                   " reason TEXT, started_ts REAL, ended_ts REAL, total_ms REAL,"
                   " replans INT, cumulative_tokens INT, plan_source TEXT)")
    legacy.execute("INSERT INTO runs VALUES (1,'b03','passed',1,'',1,2,100,0,0,'scripted')")
    legacy.commit()
    legacy.close()

    memory = Memory(db)  # миграция должна добавить human_*
    columns = {row["name"] for row in memory.conn.execute("PRAGMA table_info(runs)")}
    assert {"human_interventions", "human_wait_ms"} <= columns
    run_id = memory.start_run("b01")
    memory.finish_run(run_id, status="failed", ok=False, reason="модальный диалог: нужен человек",
                      human_interventions=1, human_wait_ms=1200.0)
    autonomy = memory.metrics()["autonomy"]
    assert autonomy["finished_runs"] == 2 and autonomy["autonomous_runs"] == 1
    assert autonomy["rate"] == 0.5 and autonomy["human_wait_ms"] == 1200.0
    memory.close()


def test_runner_marks_human_required_for_modal_reasons(tmp_path, monkeypatch):
    """Шаг с причиной «нужен человек» попадает в метрику вмешательств и в инциденты."""
    from mcp_hands.brain import runner as runner_mod
    from mcp_hands.mcp_server import ToolResult

    memory = Memory(tmp_path / "m.db")

    def stalled(**kwargs):
        return ToolResult(ok=False, reason="модальный диалог Word (bosa_sdm_msword): нужен человек")

    original = runner_mod.DISPATCH_TABLE["word_report_from_template"]
    runner_mod.DISPATCH_TABLE["word_report_from_template"] = stalled
    try:
        plan = parse_plan({"task_id": "x", "steps": [
            {"action": "word_report_from_template",
             "args": {"template": "t", "replacements": {"a": "b"}, "save_as": "s"},
             "expect": {"kind": "file_exists", "params": {"path": str(tmp_path / 'нет.docx')}}}]})
        run = run_plan(plan, RunnerLimits(attempts_per_step=1), memory=memory)
    finally:
        runner_mod.DISPATCH_TABLE["word_report_from_template"] = original
    assert not run.ok and run.steps[0].human_required
    metrics = memory.metrics()
    assert metrics["autonomy"]["rate"] == 0.0
    kinds = {row["kind"] for row in metrics["incidents"]}
    assert "human_required" in kinds, metrics["incidents"]
    memory.close()


def test_satellite_recovers_after_forced_death():
    """Рекавери ядра: после принудительной смерти сателлита следующий вызов
    сам поднимает новый процесс (это уже штатный путь probe.call)."""
    satellite = ProbeProcess(default_timeout_s=DOMAIN_OPERATION_TIMEOUT_S)
    try:
        first = satellite.ping()
        assert first.get("pid")
        satellite.kill()
        assert not satellite.alive
        second = satellite.call("ping", timeout_s=20.0)  # новый процесс поднимается сам
        assert second.get("pid") and second["pid"] != first["pid"]
    finally:
        satellite.kill()


@pytest.mark.office
def test_locked_file_is_reported_not_hidden(tmp_path):
    """Занятый монопольно файл: Word-инструмент ЧЕСТНО отказывает (без модального
    зависания), прогон failed с причиной; исходный файл пользователя не изменён."""
    from mcp_hands.mcp_server import word_report_from_template

    target = tmp_path / "занятый.docx"
    target.write_text("ВАЖНЫЙ ПОЛЬЗОВАТЕЛЬСКИЙ ФАЙЛ", encoding="utf-8")
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateFileW(str(target), 0x40000000, 0, None, 3, 0x80, None)  # WRITE, no share
    assert handle != -1, "не удалось поставить монопольную блокировку"
    try:
        # despite .docx: файл занят на запись, Word попытается открыть
        result = word_report_from_template(
            template=str(target), replacements={"{{X}}": "Y"},
            save_as=str(tmp_path / "выход.docx"))
        assert not result.ok, "занятый файл обязан отражаться отказом, а не успехом"
        assert result.reason or result.detail, "отказ без причины недопустим"
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(handle))
    assert target.read_text(encoding="utf-8") == "ВАЖНЫЙ ПОЛЬЗОВАТЕЛЬСКИЙ ФАЙЛ"
