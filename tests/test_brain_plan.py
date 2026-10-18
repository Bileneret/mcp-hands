"""Каркас реестра и плана этапа 3 (шаг 0): десять задач бенчмарка + strict-схема плана.

Тесты фиксируют контракты, на которых строится runner (закрытый цикл) и
LLM-клиент (шаг 2): белый список действий, обязательная постусловие, запрет
лишних полей, честное покрытие задач.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mcp_hands.benchmark.tasks import TASKS, coverage
from mcp_hands.brain.plan import Plan, parse_plan
from mcp_hands.mcp_server import TOOL_NAMES


def test_benchmark_registry_is_honest():
    """Десять задач DESIGN §7; ровно те, что закрываются текущим набором, готовы;
    у остальных перечислены отсутствующие инструменты — по именам, не «как-нибудь».
    Страж обновляется ОСОЗНАННО при каждом расширении набора (сейчас: колонна B —
    b08 через fs_sort_by_type + fs_trash_move)."""
    report = coverage()
    assert report["total"] == 10
    ids = {task.id for task in TASKS}
    assert len(ids) == 10 and all(tid.startswith("b") for tid in ids)
    covered = {t.id for t in TASKS if set(t.needs) <= TOOL_NAMES}
    assert covered == {"b01", "b02", "b03", "b04", "b05", "b06", "b07", "b08", "b09", "b10"}, (
        "набор инструментов изменился — обнови ожидание этого стража и реестр "
        f"в suite (scripted-планы/стенды); сейчас покрыты: {sorted(covered)}; "
        "при полном покрытии все десять задач DESIGN §7 должны быть готовы")
    for row in report["tasks"]:
        if row["ready"]:
            assert row["missing_tools"] == []
        else:
            assert row["missing_tools"], f"{row['id']}: отсутствующие должны быть названы"


def test_plan_accepts_valid_minimal_plan():
    plan = parse_plan({
        "task_id": "b03",
        "steps": [
            {"action": "word_pdf_run",
             "expect": {"kind": "scenario_passed",
                        "params": {"summary": "сценарий 1/1"}}},
        ]})
    assert isinstance(plan, Plan) and plan.steps[0].action == "word_pdf_run"


def test_plan_rejects_unknown_action():
    """Стоп-лист наследуется схемой: действие вне набора mcp-hands не проходит."""
    with pytest.raises(ValidationError) as exc:
        parse_plan({
            "task_id": "b08",
            "steps": [{"action": "delete_files_recursively",
                       "expect": {"kind": "process_gone"}}]})
    assert "delete_files_recursively" in str(exc.value)


def test_plan_requires_expectation():
    """Closed-loop обязателен: шаг без postcondition невалиден (DESIGN §4.1)."""
    with pytest.raises(ValidationError):
        parse_plan({"task_id": "b03", "steps": [{"action": "health"}]})


def test_plan_rejects_unknown_expect_kind_and_extra_fields():
    with pytest.raises(ValidationError):
        parse_plan({"task_id": "x", "steps": [
            {"action": "health", "expect": {"kind": "window_listed"}}]})
    with pytest.raises(ValidationError):
        # лишнее поле наверху и внутри шага — запрет инъекции параметров
        parse_plan({"task_id": "x", "steps": [{"action": "health", "expect": {"kind": "scenario_passed"}, "sudo": True}]})


def test_plan_rejects_broken_json_and_malformed_string():
    with pytest.raises(ValueError, match="не является JSON"):
        parse_plan("{не json}")
    with pytest.raises(ValidationError):
        parse_plan({"task_id": "x", "steps": []})  # min_length=1

