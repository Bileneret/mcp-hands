"""Тесты памяти и бенчмарк-сьюты (этап 3, шаг 3 + финал).

Живой прогон покрытой задачи (Word) — под маркером `office`; остальное —
детерминировано на временной базе.
"""

from __future__ import annotations

import pytest

from mcp_hands.benchmark.suite import run_suite
from mcp_hands.brain.plan import parse_plan
from mcp_hands.brain.runner import RunnerLimits, run_plan
from mcp_hands.memory import Memory


@pytest.fixture
def memory(tmp_path):
    mem = Memory(tmp_path / "agent_memory.db")
    yield mem
    mem.close()


def test_memory_schema_and_metrics(memory):
    """Схема и метрики: каждый провал виден в цифрах (DESIGN §7 считается запросами)."""
    run_id = memory.start_run("t1", plan_source="scripted")
    memory.record_llm_call(task_id="t1", model="m", attempt=1, prompt_tokens=100,
                           completion_tokens=50, verdict="ok")
    memory.record_incident(run_id, "guard_refused", "hotkey", "окно не то")
    memory.finish_run(run_id, status="passed", ok=True, total_ms=1000.0)
    run2 = memory.start_run("t2")
    memory.finish_run(run2, status="missing_tools", ok=False, reason="нет инструментов")
    data = memory.metrics()
    assert data["llm"]["tokens"] == 150 and data["llm"]["calls"] == 1
    assert data["incidents"] == [{"kind": "guard_refused", "n": 1}]
    statuses = {(r["task_id"], r["status"]): r["runs"] for r in data["by_task_status"]}
    assert statuses[("t1", "passed")] == 1 and statuses[("t2", "missing_tools")] == 1


def test_runner_writes_steps_and_incidents_to_memory(memory, tmp_path):
    """Провальные шаги и отказы стража попадают в память — провалы не прячутся."""
    plan = parse_plan({"task_id": "x", "steps": [
        {"action": "hotkey", "args": {"hwnd": 987654321, "keys": ["CTRL", "S"]},
         "expect": {"kind": "scenario_passed"}}]})
    run = run_plan(plan, RunnerLimits(attempts_per_step=1), memory=memory)
    assert not run.ok
    data = memory.metrics()
    assert data["failed_steps"] == 1, data
    assert {"kind": "guard_refused", "n": 1} in data["incidents"], data["incidents"]
    run_row = memory.conn.execute("SELECT status, replans FROM runs").fetchone()
    assert run_row["status"] == "failed"


def test_suite_dry_llm_records_all_selected_tasks(tmp_path):
    """Сьюта на непокрытых задачах: честный missing_tools по именам, без имитации."""
    summary = run_suite(task_ids=["b07", "b09"], db_path=tmp_path / "m.db")
    assert summary["mode"].startswith("scripted")
    by_id = {row["task_id"]: row for row in summary["tasks"]}
    assert by_id["b07"]["status"] == "missing_tools"
    assert "browser_youtube_capture" in by_id["b07"]["reason"]
    assert "excel_workbook_build" in by_id["b09"]["reason"]
    assert summary["passed"] == 0 and summary["missing_tools"] == 2


def test_suite_b08_stand_passes_or_skips_without_recycle_bin(tmp_path):
    """Задача b08 через стенд: сортировка по типам + корзина.
    Если в среде недоступна корзина, шаг корзины честно провалится — тогда скип
    с причиной (это ограничение среды, а не кода)."""
    summary = run_suite(task_ids=["b08"], db_path=tmp_path / "m.db")
    row = summary["tasks"][0]
    if row["status"] != "passed" and "корзин" not in (row.get("reason") or "").lower():
        assert row["status"] == "passed", row
    if row["status"] != "passed":
        pytest.skip(f"корзина недоступна: {row.get('reason')}")
    assert row["steps"] == 2, row


@pytest.mark.office
def test_suite_b03_scripted_passes(tmp_path):
    """Покрытая задача b03 в режиме --dry-llm: план → Word→PDF → вердикт passed,
    метрики записаны (живой прогон, Word поднимается и закрывается штатно)."""
    summary = run_suite(task_ids=["b03"], db_path=tmp_path / "m.db")
    assert summary["passed"] == 1 and summary["failed"] == 0, summary
    row = summary["tasks"][0]
    assert row["status"] == "passed" and row["steps"] == 1
    statuses = {(r["task_id"], r["status"]): r["runs"]
                for r in summary["metrics"]["by_task_status"]}
    assert statuses[("b03", "passed")] == 1, statuses


@pytest.mark.office
def test_suite_b01_b02_stands_pass(tmp_path):
    """Офисная колонна A на стендах: b01 — отчёт по шаблону (титул+структура),
    b02 — форматирование (заголовки-стили, шрифт, интервалы, оглавление).
    Оба инструмента проверяют себя перечитыванием свойств документа
    (OutlineLevel/InUse), поэтому вердикт passed — не «вызов не упал»."""
    summary = run_suite(task_ids=["b01", "b02"], db_path=tmp_path / "m.db")
    by_id = {row["task_id"]: row for row in summary["tasks"]}
    assert by_id["b01"]["status"] == "passed", by_id["b01"]
    assert by_id["b02"]["status"] == "passed", by_id["b02"]
    assert summary["failed"] == 0, summary
