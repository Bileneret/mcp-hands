"""Раннер сценариев: запись шагов, постусловий и метрик.

Отчёт по шагу содержит не только «сколько заняло», но и **чем подтверждён результат**:
сценарий обязан назвать наблюдаемый признак (файл появился, текст совпал, окно есть).
Иначе успех шага неотличим от «вызов не упал».
"""

from __future__ import annotations

import json
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

ARTIFACTS_DIR = Path(__file__).resolve().parents[2] / "logs" / "scenarios"


@dataclass
class Step:
    """Один шаг сценария: что делали, чем подтвердили, сколько заняло."""

    title: str
    expect: str = ""
    ok: bool = False
    ms: float = 0.0
    method: str = ""
    evidence: str = ""
    error: str = ""
    details: dict = field(default_factory=dict)

    def describe(self) -> dict:
        return {"step": self.title, "expect": self.expect, "ok": self.ok,
                "ms": round(self.ms, 1), "method": self.method,
                "evidence": self.evidence, "error": self.error, **self.details}


class Run:
    """Запись одного прогона сценария."""

    def __init__(self, name: str, artifacts: Path | None = None) -> None:
        self.name = name
        self.artifacts = Path(artifacts) if artifacts else ARTIFACTS_DIR / name
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.steps: list[Step] = []
        self.started = time.perf_counter()
        self._current: Step | None = None

    # --- шаги -------------------------------------------------------------

    @contextmanager
    def step(self, title: str, expect: str = "", method: str = ""):
        """Открывает шаг: время считается само, постусловие объявляет сценарий."""
        step = Step(title=title, expect=expect, method=method)
        self._current = step
        started = time.perf_counter()
        try:
            yield step
        except Exception as exc:  # noqa: BLE001 — падение шага это результат, а не конец прогона
            step.ok = False
            step.error = f"{type(exc).__name__}: {exc}"
            step.details.setdefault("traceback", traceback.format_exc()[-500:])
        finally:
            step.ms = (time.perf_counter() - started) * 1000.0
            self.steps.append(step)
            self._current = None

    def note(self, **kwargs) -> None:
        """Добавляет данные к текущему шагу."""
        if self._current is not None:
            self._current.details.update(kwargs)

    def confirm(self, evidence: str, ok: bool = True) -> None:
        """Фиксирует постусловие шага: чем именно подтверждён результат."""
        if self._current is not None:
            self._current.ok = ok
            self._current.evidence = evidence

    # --- итог -------------------------------------------------------------

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(step.ok for step in self.steps)

    def finish(self, extra: dict | None = None) -> dict:
        summary = {
            "scenario": self.name,
            "ok": self.ok,
            "steps": len(self.steps),
            "failed": [step.title for step in self.steps if not step.ok],
            "total_ms": round((time.perf_counter() - self.started) * 1000.0, 1),
            "step_ms": {step.title: round(step.ms, 1) for step in self.steps},
            "detail": [step.describe() for step in self.steps],
            **(extra or {}),
        }
        path = self.artifacts / f"run_{time.strftime('%Y%m%d_%H%M%S')}.json"
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
        summary["report"] = str(path)
        return summary


def run_scenario(scenario, repeats: int = 1, name: str = "", artifacts: Path | None = None,
                 verbose: bool = True) -> dict:
    """Прогоняет сценарий несколько раз и сводит результат.

    `scenario` — функция вида `def scenario(run: Run, workdir: Path) -> None`.
    """
    title = name or getattr(scenario, "__name__", "scenario")
    results: list[dict] = []
    for index in range(max(1, repeats)):
        workdir = (Path(artifacts) if artifacts else ARTIFACTS_DIR / title) / f"attempt_{index + 1}"
        workdir.mkdir(parents=True, exist_ok=True)
        run = Run(title, artifacts=workdir)
        if verbose:
            print(f"\n=== {title}, прогон {index + 1}/{repeats} ===")
        try:
            scenario(run, workdir)
        except Exception as exc:  # noqa: BLE001 — сценарий упал целиком
            with run.step("сценарий целиком", expect="все шаги выполнены") as step:
                step.error = f"{type(exc).__name__}: {exc}"
            if verbose:
                print(f"   сценарий упал: {type(exc).__name__}: {exc}")
        summary = run.finish({"attempt": index + 1, "workdir": str(workdir)})
        results.append(summary)
        if verbose:
            for step in run.steps:
                mark = "ок " if step.ok else "ОШИБКА"
                # Причину отказа печатаем обязательно: без неё разбор прогона невозможен.
                note = step.evidence if step.ok else (step.error or step.evidence or "без пояснения")
                print(f"   [{mark}] {step.title:<44} {step.ms:7.0f} мс  {note[:70]}")
    passed = sum(1 for item in results if item["ok"])
    return {"scenario": title, "attempts": len(results), "passed": passed,
            "failed": len(results) - passed, "results": results}
