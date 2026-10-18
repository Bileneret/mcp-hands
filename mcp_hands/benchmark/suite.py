"""Бенчмарк-сьюта (этап 3, финал): десять задач DESIGN §7 запускаются end-to-end.

Правило этапа: задачи ЗАПУСКАЮТСЯ и МЕРЯЮТСЯ; порог успеха не ставится.
Каждая задача получает честный вердикт:
- `passed` — план исполнен, постусловия подтверждены (реальный прогон через runner);
- `missing_tools` — набором инструментов задача не покрыта, отсутствующие
  названы по именам (запуск не симулируется);
- `failed` — план не прошёл (причина в памяти и в отчёте).

Режимы плана: `scripted` (скриптовый план из JSON — воспроизводимо, без сети)
и `llm` (живой план от модели; перепланирование тоже живое, каждый вызов
фиксируется в llm_calls с токенами).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ..brain.plan import parse_plan
from ..brain.runner import RunnerLimits, run_plan
from ..memory import DEFAULT_DB, Memory
from .tasks import TASKS

ARTIFACTS = Path(__file__).resolve().parents[2] / "logs" / "benchmark"

# Скриптовые планы для покрытых задач (воспроизводимость без сети).
SCRIPTED_PLANS: dict[str, dict] = {
    "b03": {"task_id": "b03", "steps": [
        {"action": "word_pdf_run", "args": {},
         "expect": {"kind": "scenario_passed", "params": {"task": "b03"}}}]},
}


def _record_missing(task, memory: Memory) -> dict:
    """Не покрытая задача: честная запись без имитации запуска."""
    run_id = memory.start_run(task.id, plan_source="none")
    reason = f"нет инструментов: {', '.join(sorted(task.needs))}" if task.needs \
        else "нет инструментов"
    memory.finish_run(run_id, status="missing_tools", ok=False, reason=reason)
    return {"task_id": task.id, "status": "missing_tools", "reason": reason}


def _live_replan(config, memory: Memory, task):
    """Живой колбэк перепланирования: контекст сбоя → модель → strict-схема."""
    from ..brain.llm import _post_chat, build_plan_messages

    def replan(plan, failed):
        messages = build_plan_messages(task.id, task.title, task.needs, task.note)
        messages.append({"role": "assistant", "content": plan.model_dump_json()})
        messages.append({"role": "user", "content":
                         "Шаг не выполнен: "
                         f"{failed.reason or 'причина не установлена'}. "
                         "Верни исправленный план для ОСТАВШЕЙСЯ части работы."})
        try:
            response = _post_chat(config, messages)
        except Exception:  # noqa: BLE001 — сеть/провайдер: перепланирование сорвано
            memory.record_incident(None, "replan_transport", failed.action,
                                   "перепланирование недоступно")
            return None
        memory.record_llm_call(task_id=task.id, model=config.model, attempt=1,
                               ms=response.ms, prompt_tokens=response.prompt_tokens,
                               completion_tokens=response.completion_tokens,
                               verdict="replan")
        try:
            return parse_plan(response.text)
        except ValueError as exc:
            memory.record_llm_call(task_id=task.id, model=config.model, attempt=1,
                                   verdict=f"replan schema: {exc}")
            return None

    return replan


def run_suite(task_ids: list[str] | None = None, live: bool = False,
              db_path: Path | str = DEFAULT_DB,
              limits: RunnerLimits | None = None) -> dict:
    """Прогоняет выбранные задачи (по умолчанию все 10) и пишет метрики в память."""
    memory = Memory(db_path)
    started = time.perf_counter()
    rows: list[dict] = []
    config = None
    if live:
        from ..brain.llm import load_config, request_plan

        config = load_config()
    try:
        for task in TASKS:
            if task_ids and task.id not in task_ids:
                continue
            if task.id not in SCRIPTED_PLANS:
                # задача не покрыта набором инструментов — честная фиксация
                rows.append(_record_missing(task, memory))
                continue
            plan_dict = SCRIPTED_PLANS[task.id]
            if live:
                result = request_plan(config, task.id, task.title, task.needs, task.note)
                for attempt in result.attempts:
                    memory.record_llm_call(
                        task_id=task.id, model=config.model,
                        attempt=int(attempt.get("attempt", 1)),
                        ms=float(attempt.get("ms", 0)),
                        prompt_tokens=int(attempt.get("prompt_tokens", 0)),
                        completion_tokens=int(attempt.get("completion_tokens", 0)),
                        verdict=str(attempt.get("verdict", attempt.get("transport_error", ""))))
                if result.plan is None:
                    run_id = memory.start_run(task.id, plan_source="llm")
                    memory.finish_run(run_id, status="failed", ok=False,
                                      reason=f"план не получен: {result.error}")
                    rows.append({"task_id": task.id, "status": "failed",
                                 "reason": result.error})
                    continue
                plan = result.plan
                source = "llm"
            else:
                plan = parse_plan(plan_dict)
                source = "scripted"
            run = run_plan(plan, limits or RunnerLimits(), memory=memory,
                           plan_source=source,
                           llm_request_plan=(
                               _live_replan(config, memory, task) if live else None))
            rows.append({"task_id": task.id, "status": "passed" if run.ok else "failed",
                         "reason": run.reason, "total_ms": round(run.total_ms, 1),
                         "replans": run.replans, "steps": len(run.steps)})
    finally:
        summary = {"mode": "llm" if live else "scripted (--dry-llm)",
                   "tasks": rows,
                   "passed": sum(1 for r in rows if r["status"] == "passed"),
                   "missing_tools": sum(1 for r in rows if r["status"] == "missing_tools"),
                   "failed": sum(1 for r in rows if r["status"] == "failed"),
                   "total_ms": round((time.perf_counter() - started) * 1000.0, 1),
                   "metrics": memory.metrics()}
        memory.close()
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    path = ARTIFACTS / f"suite_{time.strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    summary["report"] = str(path)
    return summary


def main(argv: list[str] | None = None) -> int:
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    task_ids = None
    live = "--live" in argv
    if "--tasks" in argv:
        index = argv.index("--tasks")
        task_ids = [t.strip() for t in argv[index + 1].split(",") if t.strip()]
    summary = run_suite(task_ids=task_ids, live=live)
    print(json.dumps({k: summary[k] for k in
                      ("mode", "passed", "missing_tools", "failed", "total_ms", "report")},
                     ensure_ascii=False, indent=1))
    for row in summary["tasks"]:
        mark = {"passed": "+", "missing_tools": "~", "failed": "!"}[row["status"]]
        print(f"  [{mark}] {row['task_id']}: {row['status']} {row.get('reason', '')[:80]}")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
