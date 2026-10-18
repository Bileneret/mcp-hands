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


def test_missing_tools_is_recorded_honestly(tmp_path):
    """Механизм честной фиксации непокрытых задач (юнит-уровень): пропуск
    инструмента не имитируется, а описывается именами в памяти.

    Проверяем напрямую, а не через реестр: полное покрытие десяти задач DESIGN §7
    делает «живой» пример missing-задачи невозможным — инвариант механизма
    сохраняем отдельно."""
    from mcp_hands.benchmark.suite import _record_missing
    from mcp_hands.benchmark.tasks import BenchmarkTask

    memory = Memory(tmp_path / "m.db")
    fake = BenchmarkTask("bx", "Фейковая задача", needs=["tool_которого_нет", "второй_нет"])
    row = _record_missing(fake, memory)
    assert row["status"] == "missing_tools" and "tool_которого_нет" in row["reason"]
    statuses = {(r["task_id"], r["status"]): r["runs"]
                for r in memory.metrics()["by_task_status"]}
    assert statuses[("bx", "missing_tools")] == 1, statuses
    memory.close()


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


@pytest.mark.office
def test_suite_b09_stand_passes(tmp_path):
    """Excel-стенд b09: два CSV → книга со сводом и диаграммой; инструмент
    подтверждает результат сверкой С ДИСКА (закрыть→открыть→сличить ячейки)."""
    summary = run_suite(task_ids=["b09"], db_path=tmp_path / "m.db")
    row = summary["tasks"][0]
    assert row["status"] == "passed", row
    assert summary["failed"] == 0, summary


def test_suite_browser_stands_pass(tmp_path):
    """Браузерные стенды колонны C: b06 (сессия вкладок с дубликатом → остались
    ровно keep), b04 (источники со страницы → CSV), b07 (медиа-стенд: скорость
    и таймкоды → заметки). Внешний веб не используется; при отсутствии Edge
    тест пропускается с причиной."""
    from mcp_hands.scenarios.browser_links import edge_path

    if not edge_path():
        pytest.skip("не найден msedge.exe — браузерные стенды недоступны в этой среде")
    summary = run_suite(task_ids=["b06", "b04", "b07"], db_path=tmp_path / "m.db")
    by_id = {row["task_id"]: row for row in summary["tasks"]}
    for task in ("b06", "b04", "b07"):
        assert by_id[task]["status"] == "passed", by_id[task]
    assert summary["failed"] == 0, summary
