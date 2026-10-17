"""Тесты runner замкнутого цикла (этап 3, шаг 1).

Правка аудитора v15 в основе: статический DISPATCH_TABLE (без getattr),
фильтрация args (лишние — в ignored_args, нехватка обязательных — фатальный
отказ), лимиты задачи (replans/токены), постусловия против реального мира.
"""

from __future__ import annotations

import inspect
import os

from mcp_hands.brain import runner as runner_mod
from mcp_hands.brain.plan import parse_plan
from mcp_hands.brain.runner import DISPATCH_TABLE, RunnerLimits, run_plan
from mcp_hands.mcp_server import TOOL_NAMES


def test_dispatch_table_is_exact_and_static():
    """Defense-in-depth (аудит v15): таблица ровно канон и в исходнике нет getattr."""
    assert set(DISPATCH_TABLE) == set(TOOL_NAMES)
    assert DISPATCH_TABLE["health"] is not None
    source = inspect.getsource(runner_mod)
    assert "getattr(" not in source.replace("getattr(action_fn, \"__name__\", \"\")", ""), (
        "динамические вызовы запрещены; допускается только чтение __name__")
    for name, fn in DISPATCH_TABLE.items():
        assert getattr(fn, "__name__") == name, f"таблица размечена по именам: {name}"


def _plan(steps: list[dict]) -> "object":
    return parse_plan({"task_id": "x", "steps": steps})


def test_extra_args_recorded_not_fatal():
    """Безобидный шум модели (лишние args у безпараметрового инструмента)
    отбрасывается и попадает в отчёт шага — план не валится."""
    plan = _plan([{"action": "health",
                   "args": {"scenario": "word_to_pdf", "name_template": "{x}.pdf"},
                   "expect": {"kind": "process_gone", "params": {"pid": 1}}}])
    run = run_plan(plan, RunnerLimits(attempts_per_step=1))
    step = run.steps[0]
    assert run.ok and step.ignored_args == ["name_template", "scenario"], step
    assert "pid" not in step.ignored_args  # expect-параметры не соперничают с args


def test_missing_required_args_is_fatal():
    """Параметризованный инструмент без обязательных параметров — фатальный отказ
    шага (аудит v15), бе повторов."""
    plan = _plan([{"action": "click_node",
                   "args": {"node": {"name": "alpha"}},  # нет hwnd
                   "expect": {"kind": "element_exists", "params": {"hwnd": 1}}}])
    run = run_plan(plan, RunnerLimits(attempts_per_step=3))
    step = run.steps[0]
    assert not run.ok and not step.ok
    assert "обязательных параметров" in step.reason and "hwnd" in step.reason, step.reason


def test_expect_file_contains_and_process_gone(tmp_path):
    """Проверки против реального мира: файл и живой/мёртвый процесс (в обоих направлениях)."""
    file = tmp_path / "evidence.txt"
    file.write_text("MCP-Hands контрольный отпечаток", encoding="utf-8")
    plan = _plan([
        {"action": "health",
         "args": {},
         "expect": {"kind": "file_contains",
                    "params": {"path": str(file), "needle": "отпечаток"}}},
 ])
    run = run_plan(plan, RunnerLimits(attempts_per_step=1))
    assert run.ok and run.steps[-1].expect_ok

    gone_ok, evidence = runner_mod._check_expect(
        "process_gone", {"pid": 4_000_000})  # заведомо несуществующий
    assert gone_ok, evidence
    alive_ok, _evidence = runner_mod._check_expect(
        "process_gone", {"pid": os.getpid()})
    assert alive_ok is False, "живой процесс обязан считаться существующим"


def test_refused_action_stops_step_without_retries():
    """Отказ стража повторами не лечится — шаг завершается сразу (attempts=1):
    hotkey при неактивном ожидаемом окне обязан быть отсечён стражем."""
    plan = _plan([{"action": "hotkey",
                   "args": {"hwnd": 987654321, "keys": ["CTRL", "S"]},
                   "expect": {"kind": "scenario_passed"}}])
    run = run_plan(plan, RunnerLimits(attempts_per_step=3))
    step = run.steps[0]
    assert not step.ok and step.attempts == 1, step
    assert "окно" in (step.reason or "") or "активн" in (step.reason or ""), step.reason


def test_dry_llm_mode_cannot_replan():
    """--dry-llm: колбэка перепланирования нет — после бюджета повторов честный
    стоп с причиной, replans остаются нулём."""
    plan = _plan([{"action": "window_activate",
                   "args": {"hwnd": 999999999},  # окна нет: action вернёт ok=False
                   "expect": {"kind": "process_gone", "params": {"pid": 1}}}])
    run = run_plan(plan, RunnerLimits(attempts_per_step=2))
    assert not run.ok and run.replans == 0
    assert len(run.steps) == 1 and run.steps[0].attempts == 2
    assert run.reason, "обязательна зафиксированная причина неуспеха"


def test_replan_budget_limits_task(tmp_path, monkeypatch):
    """Лимиты задачи (аудит v15): max_task_replans=2 → третий вызов перепланирования
    не делается; замена плана продолжает работу с нового плана (контракт)."""
    plan_v1 = _plan([{"action": "window_activate", "args": {"hwnd": 999999999},
                      "expect": {"kind": "process_gone", "params": {"pid": 1}}}])
    plan_v2 = parse_plan({"task_id": "x", "steps": [
        {"action": "health", "args": {},
         "expect": {"kind": "file_contains",
                    "params": {"path": str(tmp_path / "ok.txt"),
                               "needle": "перепланирована"}}}]})
    (tmp_path / "ok.txt").write_text("задача перепланирована успешно", encoding="utf-8")

    replan_requests: list = []

    def fake_replan(current_plan, failed_outcome):
        replan_requests.append(failed_outcome.action)
        return plan_v2

    run = run_plan(plan_v1, RunnerLimits(attempts_per_step=1, max_task_replans=2),
                   llm_request_plan=fake_replan)
    assert run.ok and len(replan_requests) == 1, run.summary()
    assert run.replans == 1 and run.steps[-1].expect_ok
    assert "перепланирована" in (tmp_path / "ok.txt").read_text(encoding="utf-8")


def test_replan_budget_exhaustion_stops():
    """Планы перепланирования, которые всё равно падают, упираются в лимит задачи."""
    plan = parse_plan({"task_id": "x", "steps": [
        {"action": "window_activate", "args": {"hwnd": 999999999},
         "expect": {"kind": "process_gone", "params": {"pid": 1}}}]})
    calls: list = []

    def endless_replan(_plan, _outcome):
        calls.append(1)
        return plan  # один и тот же падающий план

    run = run_plan(plan, RunnerLimits(attempts_per_step=1, max_task_replans=2),
                   llm_request_plan=endless_replan)
    assert not run.ok and run.replans == 2 and len(calls) == 2, run.summary()
    assert run.reason


def test_scenario_passed_predicate_requires_full_success(tmp_path):
    """scenario_passed считается выполненной только когда доменный инструмент
    отчитался «сценарий 1/1» (а не просто ok)."""
    from mcp_hands.mcp_server import ToolResult

    ok_run = ToolResult(ok=True, summary="сценарий 1/1")
    (tmp_path / "f.txt").write_text("-", encoding="utf-8")
    # проверяем через runner.plan: единственный способ вызвать с постусловием
    plan = parse_plan({"task_id": "x", "steps": [
        {"action": "word_pdf_run",
         "args": {},
         "expect": {"kind": "scenario_passed", "params": {"task": "b03"}}}]})
    original = runner_mod.DISPATCH_TABLE["word_pdf_run"]
    runner_mod.DISPATCH_TABLE["word_pdf_run"] = lambda: ok_run
    try:
        run = run_plan(plan, RunnerLimits(attempts_per_step=1))
    finally:
        runner_mod.DISPATCH_TABLE["word_pdf_run"] = original
    assert run.ok and run.steps[-1].expect_ok, run.summary()
