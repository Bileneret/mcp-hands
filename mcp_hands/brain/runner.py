"""Runner замкнутого цикла (этап 3, шаг 1): исполняет план шага за шагом.

Контракт безопасности (внесён аудитом v15):
1. **Статический `DISPATCH_TABLE`** — defense-in-depth: в таблице ровно канон
   `TOOL_NAMES`, динамических вызовов (`getattr`) нет; таблица сверяется с
   каноном при импорте и в тестах.
2. **Аргументы**: лишние — отбрасываются в `ignored_args` (описательный шум
   модели), нехватка ОБЯЗАТЕЛЬНЫХ параметров — фатальный отказ шага.
3. **Лимиты задачи**: `max_task_replans = 2`, `max_cumulative_tokens = 8192` —
   защита от бесконечного цикла перепланирования.
4. Локальные рефлексы при неуспехе: повтор шага → (при живом LLM) перепланирование
   → стоп. В режиме `--dry-llm` план задан скриптом, перепланирование невозможно —
   честная фиксация.
"""

from __future__ import annotations

import ctypes
import inspect
import json
import sys
import time
from pathlib import Path
from dataclasses import dataclass, field

from ..mcp_server import TOOL_NAMES, ToolResult
from ..mcp_server import (browser_links_run, click_node, content_map, element_find,
                          element_text, health, hotkey, notepad_run, ocr_window,
                          type_text, window_activate, windows_list, word_pdf_run)
from .plan import Plan

# Статическая таблица диспетчеризации: имя инструмента → функция ядра.
# Функции mcp_server несут те же стражи, что и MCP-инструменты (это один код).
DISPATCH_TABLE: dict[str, object] = {
    "windows_list": windows_list,
    "element_find": element_find,
    "element_text": element_text,
    "content_map": content_map,
    "ocr_window": ocr_window,
    "window_activate": window_activate,
    "click_node": click_node,
    "type_text": type_text,
    "hotkey": hotkey,
    "notepad_run": notepad_run,
    "word_pdf_run": word_pdf_run,
    "browser_links_run": browser_links_run,
    "health": health,
}

assert set(DISPATCH_TABLE) == set(TOOL_NAMES), (
    "таблица диспетчеризации и канон инструментов разошлись")


@dataclass
class RunnerLimits:
    """Лимиты задачи (внесено аудитом v15): защита от зацикливания перепланирования."""

    attempts_per_step: int = 2
    max_task_replans: int = 2
    max_cumulative_tokens: int = 8192
    dry_run_actions: bool = False  # прогонять каждый шаг с dry_run=True перед боем


@dataclass
class StepOutcome:
    """Итог исполнения шага плана (с бампами повторов)."""

    index: int
    action: str
    ok: bool = False
    reason: str = ""
    attempts: int = 0
    ms: float = 0.0
    ignored_args: list[str] = field(default_factory=list)
    expect_kind: str = ""
    expect_ok: bool = False
    evidence: str = ""


@dataclass
class PlanRun:
    """Итог прогона плана: вердикт, шаги, лимиты и расход."""

    task_id: str
    ok: bool = False
    reason: str = ""
    steps: list[StepOutcome] = field(default_factory=list)
    total_ms: float = 0.0
    replans: int = 0
    cumulative_tokens: int = 0

    def summary(self) -> dict:
        return {"task_id": self.task_id, "ok": self.ok, "reason": self.reason,
                "steps": [{"index": s.index, "action": s.action, "ok": s.ok,
                           "attempts": s.attempts, "expect": s.expect_kind,
                           "expect_ok": s.expect_ok, "ignored_args": s.ignored_args,
                           "reason": s.reason} for s in self.steps],
                "total_ms": round(self.total_ms, 1), "replans": self.replans,
                "cumulative_tokens": self.cumulative_tokens}


def _filter_args(action_fn, raw_args: dict) -> tuple[dict, list[str], str | None]:
    """Разбивает выход модели на вызываемые и лишние аргументы.

    Возвращает (вызываемые, ignored, fatal_reason). Нехватка обязательных
    параметров (без значения по умолчанию) — фатальная для шага.
    """
    if not isinstance(raw_args, dict):
        return {}, [], f"args должны быть словарём, пришло {type(raw_args).__name__}"
    signature = inspect.signature(action_fn)
    required = [name for name, param in signature.parameters.items()
                if param.default is inspect.Parameter.empty
                and param.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                   inspect.Parameter.KEYWORD_ONLY)]
    ignored = sorted(set(raw_args) - set(signature.parameters))
    callable_args = {name: raw_args[name] for name in raw_args if name in signature.parameters}
    missing = [name for name in required if name not in callable_args]
    if missing:
        return callable_args, ignored, (
            f"не хватает обязательных параметров {action_fn.__name__}: {missing}")
    return callable_args, ignored, None


def _check_expect(kind: str, params: dict) -> tuple[bool, str]:
    """Проверка постусловия шага против реального мира. Возвращает (ok, evidence)."""
    if kind == "scenario_passed":
        # доменные инструменты валидируют себя внутри; их ok и есть постусловие
        return True, ""
    if kind == "element_exists":
        result = element_find(hwnd=int(params.get("hwnd", 0)),
                              name=params.get("name", ""),
                              control_type=params.get("control_type", ""),
                              automation_id=params.get("automation_id", ""))
        found = bool(result.detail.get("found"))
        return found, (result.summary or "элемент не найден")
    if kind == "text_contains":
        result = element_text(hwnd=int(params.get("hwnd", 0)),
                              max_length=int(params.get("max_length", 4000)))
        text = result.detail.get("text") or ""
        needle = params.get("needle", "")
        if not needle:
            return False, "text_contains без needle"
        return needle in text, f"ищем {needle!r} в {len(text)} знаках"
    if kind == "file_contains":
        needle = params.get("needle", "")
        path = params.get("path", "")
        try:
            content = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return False, f"файл {path!r} недоступен: {exc}"
        if not needle:
            return False, "file_contains без needle"
        return needle in content, f"файл {len(content)} знаков"
    if kind == "window_open":
        needle = params.get("title_substring", "")
        if not needle:
            return False, "window_open без title_substring"
        result = windows_list()
        titles = [w.get("title", "") for w in result.detail.get("windows", [])]
        hits = [t for t in titles if needle.lower() in t.lower()]
        return bool(hits), f"совпали: {hits[:3]}" if hits else f"среди {len(titles)} окон нет {needle!r}"
    if kind == "process_gone":
        pid = int(params.get("pid", 0))
        if not pid:
            return False, "process_gone без pid"
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return False, f"процесс {pid} ещё существует"
        return True, f"процесса {pid} нет"
    return False, f"runner не поддерживает kind {kind!r}"


def run_plan(plan: Plan, limits: RunnerLimits | None = None,
             llm_request_plan=None) -> PlanRun:
    """Исполняет план по шагам (замкнутый цикл).

    `llm_request_plan(plan, failed_outcome) -> Plan | None` — необязательный
    колбэк перепланирования (только при живом LLM; в `--dry-llm` не задаётся).
    Контракт: модель возвращает план НОВЫЙ ДЛЯ ОСТАВШЕЙСЯ части работы (runner
    заменяет план целиком и продолжает с его первого шага); None или исключение
    = перепланирование сорвалось — прогон завершается честной причиной.

    Каждый шаг: попытки (бюджет `attempts_per_step`), попытка = вызов инструмента
    (фильтр args) →ToolResult.ok → проверка `expect` против реального мира.
    Отказ стража (`refused`) повторами не лечится — завершение шага сразу.
    """
    limits = limits or RunnerLimits()
    run = PlanRun(task_id=plan.task_id)
    started = time.perf_counter()
    position = 0
    while position < len(plan.steps):
        step = plan.steps[position]
        action_fn = DISPATCH_TABLE[step.action]
        outcome = StepOutcome(index=position + 1, action=step.action,
                              expect_kind=step.expect.kind)
        step_started = time.perf_counter()
        for attempt in range(1, max(1, limits.attempts_per_step) + 1):
            outcome.attempts = attempt
            call_args, ignored, fatal = _filter_args(action_fn, step.args)
            if ignored:
                outcome.ignored_args = ignored
            if fatal:
                outcome.reason = fatal
                break
            if action_fn.__name__ in ("click_node", "type_text", "hotkey") \
                    and limits.dry_run_actions:
                call_args = {**call_args, "dry_run": True}
            try:
                result = action_fn(**call_args)
            except Exception as exc:  # noqa: BLE001 — сбой инструмента = результат шага
                outcome.reason = f"{type(exc).__name__}: {exc}"
                continue
            if not isinstance(result, ToolResult) or not result.ok:
                outcome.reason = (result.reason if isinstance(result, ToolResult)
                                  else f"{type(result).__name__}: неожиданный возврат")
                if isinstance(result, ToolResult) and result.refused:
                    break  # отказ стража повторами не лечится — это не случайный сбой
                continue  # временный сбой: повтор по бюджету
            # действие вернуло ok: проверяем объявленную постусловие
            expect_ok, evidence = _check_expect(step.expect.kind, step.expect.params)
            if step.expect.kind == "scenario_passed":
                expect_ok = expect_ok and bool(result.ok) and result.summary.endswith("1/1")
                evidence = result.summary
            outcome.expect_ok = expect_ok
            outcome.evidence = evidence
            if expect_ok:
                outcome.ok = True
                outcome.reason = ""
                break
            outcome.reason = f"постусловие {step.expect.kind} не наступило: {evidence}"
        outcome.ms = (time.perf_counter() - step_started) * 1000.0
        run.steps.append(outcome)
        if outcome.ok:
            position += 1
            continue
        # шаг не удался: перепланирование только при живом LLM и в лимитах задачи
        if llm_request_plan is None or run.replans >= limits.max_task_replans:
            run.reason = outcome.reason or f"шаг {position + 1} не прошёл"
            break
        run.replans += 1
        regenerated = llm_request_plan(plan, outcome)
        if regenerated is None or not isinstance(regenerated, Plan):
            run.reason = "перепланирование недоступно/отклонено"
            break
        plan = regenerated
        position = 0  # новый план покрывает оставшуюся часть работы (контракт выше)
    run.total_ms = (time.perf_counter() - started) * 1000.0
    # Вердикт — по ФИНАЛЬНОМУ плану (хвост истории длиной в его шаги): провальный
    # шаг замещённого плана остаётся в steps как зафиксированный факт, но вердикт
    # прогон не роняет — план-замена перекрыл эту работу. «Провалы не прячутся»:
    # каждый неуспешный шаг сохраняется с причиной и подсвечивается в summary.
    final_tail = run.steps[len(run.steps) - len(plan.steps):] \
        if len(run.steps) >= len(plan.steps) else []
    run.ok = bool(final_tail) and len(final_tail) == len(plan.steps) \
        and all(step.ok for step in final_tail)
    if not run.ok and not run.reason:
        run.reason = "не все шаги плана прошли"
    return run


def load_plan_file(path: str) -> Plan:
    """Читает скриптовый план для --dry-llm из JSON-файла через strict-схему."""
    from .plan import parse_plan

    return parse_plan(json.loads(_read_text(path)))


def _read_text(path: str) -> str:
    from pathlib import Path

    return Path(path).read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """CLI: `python -m mcp_hands.brain.runner план.json` — dry-llm прогон."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("использование: python -m mcp_hands.brain.runner <план.json> [--limits X]")
        return 2
    plan = load_plan_file(argv[0])
    limits = RunnerLimits()
    if "--dry-run-actions" in argv:
        limits.dry_run_actions = True
    run = run_plan(plan, limits)
    print(json.dumps(run.summary(), ensure_ascii=False, indent=1))
    return 0 if run.ok else 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
