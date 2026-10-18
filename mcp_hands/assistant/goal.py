"""Режим цели: владелец называет ЦЕЛЬ, ассистент сам выбирает шаги.

Цикл (DESIGN §4.1, план `docs/PLAN_GOAL_MODE.md`, шаг 1):

    цель → observe (разведка) → план (строгая схема brain/plan.py)
        → исполнение brain/runner.py (постусловия против мира, лимиты, откаты)
        → отчёт голосом (что сделано и чем подтверждено / где застрял)

Две экономии, заложенные в замысел («думать редко, делать часто»):

* **разведка перед планом** — модели уходит компактный срез обстановки
  (`observe`), поэтому план строится по фактам, а не по догадкам;
* **память рецептов** — удачный план сохраняется под нормализованной формулировкой
  цели; повторная цель сначала пробует рецепт (без обращения к модели) и лишь при
  расхождении с миром планирует заново.

Веса моделей не обучаются: «обучение» здесь — рецепты и постусловия.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

from ..brain.llm import LLMConfig, request_plan
from ..brain.plan import Plan
from ..brain.runner import RunnerLimits, run_plan

#: Ограничения цели: шаги, повторы и потолок токенов на перепланирование.
DEFAULT_LIMITS = RunnerLimits()

#: Как звучит подтверждённый факт в голосовом отчёте (по видам постусловий).
EXPECT_SAY = {
    "video_playing": "видео играет",
    "fullscreen": "полный экран включён",
    "page_matches": "нужная страница открыта",
    "text_on_screen": "текст виден на экране",
    "element_exists": "элемент на месте",
    "text_contains": "текст окна совпал",
    "file_exists": "файл создан",
    "file_contains": "содержимое файла совпало",
    "window_open": "окно открыто",
    "window_visible": "окно на виду",
    "foreground_is": "окно в фокусе",
    "no_files_in": "папка пуста",
    "paths_gone": "пути убраны",
    "process_gone": "процесс завершён",
    "scenario_passed": "сценарий прошёл проверку",
}


def normalize_goal(text: str) -> str:
    """Ключ рецепта: регистр, знаки и двойные пробелы не должны мешать совпадению."""
    lowered = re.sub(r"[^\w\s]", " ", text.lower())
    return re.sub(r"\s+", " ", lowered).strip()


class RecipeStore:
    """Память удачных планов: цель → шаги, которые уже сработали.

    Хранилище рядом с памятью диалога (SQLite), поэтому рецепты переживают
    перезапуск ассистента. Рецепт — не догма: перед использованием он всё равно
    исполняется с постусловиями, и при расхождении с миром заменяется свежим планом.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS recipes ("
                " goal TEXT PRIMARY KEY, plan TEXT NOT NULL, uses INTEGER DEFAULT 0,"
                " ok INTEGER DEFAULT 0, updated REAL)")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def get(self, goal: str) -> Plan | None:
        with self._connect() as connection:
            row = connection.execute("SELECT plan FROM recipes WHERE goal = ?",
                                     (normalize_goal(goal),)).fetchone()
        if row is None:
            return None
        try:
            return Plan.model_validate(json.loads(row["plan"]))
        except Exception:  # noqa: BLE001 — испорченный рецепт не должен ломать цель
            return None

    def note_use(self, goal: str) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE recipes SET uses = uses + 1 WHERE goal = ?",
                               (normalize_goal(goal),))

    def save(self, goal: str, plan: Plan) -> None:
        payload = json.dumps(plan.model_dump(), ensure_ascii=False)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO recipes (goal, plan, uses, ok, updated) VALUES (?, ?, 0, 1, ?)"
                " ON CONFLICT(goal) DO UPDATE SET plan = excluded.plan,"
                " ok = 1, updated = excluded.updated",
                (normalize_goal(goal), payload, time.time()))

    def drop(self, goal: str) -> None:
        """Рецепт не сработал — забываем его, чтобы не повторять ошибку."""
        with self._connect() as connection:
            connection.execute("DELETE FROM recipes WHERE goal = ?",
                               (normalize_goal(goal),))

    def count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM recipes").fetchone()[0])


class GoalRunner:
    """Исполнение цели: разведка → план (или рецепт) → runner → отчёт голосом."""

    def __init__(self, config: LLMConfig, log_dir: Path, memory=None,
                 planner=request_plan, executor=run_plan,
                 limits: RunnerLimits | None = None) -> None:
        self.config = config
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.recipes = RecipeStore(self.log_dir / "jarvis_memory.db")
        self.memory = memory
        self.planner = planner        # инъекция для тестов: (config, ...) -> PlanRequestResult
        self.executor = executor      # инъекция для тестов: (plan, ...) -> PlanRun
        self.limits = limits or DEFAULT_LIMITS
        self.trace: list[dict] = []

    def _log(self, kind: str, **fields) -> None:
        entry = {"ts": round(time.time(), 3), "kind": kind, **fields}
        self.trace.append(entry)
        with (self.log_dir / "jarvis_goal_trace.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # --- шаги ---------------------------------------------------------------

    def observe(self) -> dict:
        """Разведка: компактный срез обстановки (без него план строится вслепую)."""
        from ..mcp_server import observe as observe_tool

        result = observe_tool()
        facts = result.detail if isinstance(result.detail, dict) else {}
        self._log("observe", summary=result.summary[:160])
        return {"summary": result.summary, "facts": facts}

    def make_plan(self, goal: str, facts: dict) -> tuple[Plan | None, str]:
        """План под цель: сначала рецепт, затем модель (со срезом обстановки)."""
        known = self.recipes.get(goal)
        if known is not None:
            self.recipes.note_use(goal)
            self._log("recipe_hit", goal=goal[:120], steps=len(known.steps))
            return known, "recipe"
        note = self._facts_note(facts)
        request = self.planner(self.config, task_id=f"goal-{abs(hash(goal)) % 100000}",
                               title=goal, needs=[], note=note)
        if request.plan is None:
            self._log("plan_failed", goal=goal[:120], error=request.error[:200])
            return None, request.error or "модель не вернула план"
        return request.plan, "llm"

    @staticmethod
    def _facts_note(facts: dict) -> str:
        """Срез обстановки для планировщика: коротко и по делу (бюджет знаков)."""
        active = (facts or {}).get("active") or {}
        lines = [line.get("text", "") for line in (facts or {}).get("lines", [])[:12]]
        parts = [
            f"активное окно: {str(active.get('title', ''))[:60]!r} ({active.get('exe', '')})",
            f"окон всего: {(facts or {}).get('windows_total', 0)}",
        ]
        occlusion = active.get("occlusion") or {}
        if occlusion:
            parts.append(f"перекрытие окна: {occlusion.get('covered')}, "
                         f"доля {occlusion.get('share')}")
        browser = (facts or {}).get("browser") or {}
        parts.append(f"живой браузер: режим {browser.get('mode') or 'не подключён'}, "
                     f"вкладка {str(browser.get('tab_url', ''))[:60] or '—'}")
        if lines:
            parts.append("видимый текст: " + " | ".join(text[:40] for text in lines[:8]))
        return "; ".join(parts)[:1200]

    # --- исполнение ---------------------------------------------------------

    def run(self, goal: str, planner_retries: int = 1) -> dict:
        """Выполнить цель. Возвращает {"say", "ok", "detail"} — фразу для голоса и факты."""
        started = time.perf_counter()
        facts = self.observe()
        plan, source = self.make_plan(goal, facts["facts"])
        if plan is None:
            say = f"Не смог построить план: {source[:120]}"
            return {"say": say, "ok": False, "detail": {"error": source, "source": "none"}}

        attempt = 0
        while True:
            attempt += 1
            outcome = self.executor(plan, limits=self.limits, memory=self.memory,
                                    plan_source=source)
            summary = outcome.summary()
            self._log("run", goal=goal[:120], source=source, ok=summary.get("ok"),
                      steps=len(plan.steps), seconds=round(time.perf_counter() - started, 1))
            if summary.get("ok"):
                self.recipes.save(goal, plan)
                return {"say": self._say_ok(summary), "ok": True,
                        "detail": {"source": source, "steps": len(plan.steps),
                                   "seconds": round(time.perf_counter() - started, 1),
                                   "summary": summary}}
            if attempt > planner_retries:
                if source == "recipe":
                    self.recipes.drop(goal)  # рецепт устарел: мир изменился
                return {"say": self._say_fail(summary), "ok": False,
                        "detail": {"source": source, "attempts": attempt,
                                   "summary": summary}}
            self._log("replan", goal=goal[:120], reason=str(summary.get("reason", ""))[:160])
            replanned, source = self.make_plan(goal, facts["facts"])
            if replanned is None:
                return {"say": self._say_fail(summary), "ok": False,
                        "detail": {"source": "none", "summary": summary}}
            plan = replanned

    @staticmethod
    def _say_ok(summary: dict) -> str:
        """Отчёт по факту: сколько шагов и какое постусловие подтвердилось."""
        steps = summary.get("steps") or []
        done = [step for step in steps if step.get("ok")]
        last = steps[-1] if steps else {}
        fact = EXPECT_SAY.get(str(last.get("expect", "")), "")
        base = f"Готово: шагов {len(done)} из {len(steps)}"
        return f"{base}, {fact}." if fact else f"{base}."

    @staticmethod
    def _say_fail(summary: dict) -> str:
        """Отчёт о провале: где именно и почему (без «что-то пошло не так»)."""
        failed = next((step for step in (summary.get("steps") or [])
                       if not step.get("ok")), None)
        reason = str(summary.get("reason", "") or "").strip()
        if failed is None:
            return f"Не получилось: {reason[:160] or 'причина не сообщена'}"
        detail = reason or str(failed.get("reason", "") or "")[:160]
        return (f"Не получилось на шаге {failed.get('index')} "
                f"({failed.get('action')}): {detail or 'причина не сообщена'}")
