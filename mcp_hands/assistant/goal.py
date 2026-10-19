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


#: Служебный шум, который TTS зачитывает как техническую пунктуацию: windows-пути
#: и JSON-фрагменты. Замечание аудита v25: голосовой отчёт должен звучать по-человечески.
_PATH_NOISE = re.compile(r"[A-Za-z]:\\[^\s,;]+")


def speakable(text: str, limit: int = 170) -> str:
    """Чистит строку для произнесения: без фигурных скобок, путей и лишних знаков.

    Скобки вырезаются от первой открывающей до последней закрывающей — вложенный JSON
    целиком, а не по кускам: иначе в речь попадал мусор вида `{"detail": }`.
    """
    clean = str(text)
    while "{" in clean and "}" in clean:
        start, end = clean.find("{"), clean.rfind("}")
        clean = clean[:start] + " " + clean[end + 1:] if end > start else \
            clean.replace("{", " ").replace("}", " ")
    clean = _PATH_NOISE.sub(" ", clean)
    clean = re.sub(r"\s+", " ", clean).strip(" .,;:—-\"'")
    return clean[:limit]


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
                " ok INTEGER DEFAULT 0, updated REAL, fingerprint TEXT DEFAULT '')")
            columns = {row["name"] for row in
                       connection.execute("PRAGMA table_info(recipes)").fetchall()}
            if "fingerprint" not in columns:  # миграция старых баз
                connection.execute("ALTER TABLE recipes ADD COLUMN fingerprint TEXT DEFAULT ''")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def get(self, goal: str) -> tuple[Plan, dict] | None:
        """Рецепт вместе с отпечатком обстановки, при которой он сработал."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT plan, fingerprint FROM recipes WHERE goal = ?",
                (normalize_goal(goal),)).fetchone()
        if row is None:
            return None
        try:
            plan = Plan.model_validate(json.loads(row["plan"]))
        except Exception:  # noqa: BLE001 — испорченный рецепт не должен ломать цель
            return None
        try:
            fingerprint = json.loads(row["fingerprint"] or "{}")
        except json.JSONDecodeError:
            fingerprint = {}
        return plan, (fingerprint if isinstance(fingerprint, dict) else {})

    def note_use(self, goal: str) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE recipes SET uses = uses + 1 WHERE goal = ?",
                               (normalize_goal(goal),))

    def save(self, goal: str, plan: Plan, fingerprint: dict | None = None) -> None:
        payload = json.dumps(plan.model_dump(), ensure_ascii=False)
        stamp = json.dumps(fingerprint or {}, ensure_ascii=False)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO recipes (goal, plan, uses, ok, updated, fingerprint)"
                " VALUES (?, ?, 0, 1, ?, ?)"
                " ON CONFLICT(goal) DO UPDATE SET plan = excluded.plan,"
                " ok = 1, updated = excluded.updated, fingerprint = excluded.fingerprint",
                (normalize_goal(goal), payload, time.time(), stamp))

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
        self.last_plan_tokens = 0  # расход планировщика последнего плана
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

    @staticmethod
    def fingerprint(facts: dict) -> dict:
        """Отпечаток обстановки: окно и режим браузера, при которых цель решалась.

        Нужен, чтобы рецепт не срабатывал «в чужом окне» и не удалялся из-за этого
        (замечание аудита v25): при несовпадении отпечатка просто планируем заново.
        """
        active = (facts or {}).get("active") or {}
        browser = (facts or {}).get("browser") or {}
        return {"exe": str(active.get("exe", ""))[:40],
                "browser": str(browser.get("mode", ""))}

    def make_plan(self, goal: str, facts: dict) -> tuple[Plan | None, str]:
        """План под цель: сначала рецепт (если обстановка совпала), затем модель."""
        current = self.fingerprint(facts)
        known = self.recipes.get(goal)
        if known is not None:
            plan, stamp = known
            if not stamp or all(stamp.get(key) in ("", None, current.get(key))
                                for key in ("exe", "browser")):
                self.recipes.note_use(goal)
                self._log("recipe_hit", goal=goal[:120], steps=len(plan.steps))
                return plan, "recipe"
            self._log("recipe_skip", goal=goal[:120], was=stamp, now=current)
        note = self._facts_note(facts)
        request = self.planner(self.config, task_id=f"goal-{abs(hash(goal)) % 100000}",
                               title=goal, needs=[], note=note)
        tokens = sum(int(item.get("prompt_tokens", 0)) + int(item.get("completion_tokens", 0))
                     for item in getattr(request, "attempts", []) or [])
        self.last_plan_tokens = tokens  # расход планировщика — в отчёт миссий
        if request.plan is None:
            self._log("plan_failed", goal=goal[:120], error=request.error[:200], tokens=tokens)
            return None, request.error or "модель не вернула план"
        self._log("plan", goal=goal[:120], steps=len(request.plan.steps), tokens=tokens)
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

        # многошаговые цели (браузерная лестница) заслуживают второго переплана:
        # замечание аудита v25 — у одной попытки слишком узкий бюджет для 3+ шагов
        retries = 2 if len(plan.steps) >= 3 else planner_retries
        attempt = 0
        while True:
            attempt += 1
            outcome = self.executor(plan, limits=self.limits, memory=self.memory,
                                    plan_source=source)
            summary = outcome.summary()
            self._log("run", goal=goal[:120], source=source, ok=summary.get("ok"),
                      steps=len(plan.steps), seconds=round(time.perf_counter() - started, 1),
                      tokens=self.last_plan_tokens)
            if summary.get("ok"):
                self.recipes.save(goal, plan, self.fingerprint(facts["facts"]))
                active = str(((facts["facts"].get("active") or {}).get("title")) or "")
                return {"say": self._say_ok(summary, active_title=active), "ok": True,
                        "detail": {"source": source, "steps": len(plan.steps),
                                   "seconds": round(time.perf_counter() - started, 1),
                                   "tokens": self.last_plan_tokens, "summary": summary}}
            if attempt > retries:
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

    #: Цели-наблюдения: их отчёт обязан называть окно, иначе «что на экране?»
    #: не отвечает на свой вопрос (нашёл живой прогон миссии 1).
    OBSERVING_ACTIONS = frozenset({"observe", "ocr_window", "ocr_find", "windows_list"})

    @staticmethod
    def _say_ok(summary: dict, active_title: str = "") -> str:
        """Отчёт по факту: окно (для наблюдений), сколько шагов и что подтвердилось."""
        steps = summary.get("steps") or []
        done = [step for step in steps if step.get("ok")]
        last = steps[-1] if steps else {}
        fact = EXPECT_SAY.get(str(last.get("expect", "")), "")
        content = speakable(str(last.get("summary", "") or ""), limit=120)
        head = ""
        if active_title and str(last.get("action", "")) in GoalRunner.OBSERVING_ACTIONS:
            head = f"Активное окно: {speakable(active_title, limit=60)}. "
        base = f"Готово: шагов {len(done)} из {len(steps)}"
        if content:
            return speakable(f"{head}{base}, {fact}. {content}" if fact
                             else f"{head}{base}. {content}")
        return speakable(f"{head}{base}, {fact}." if fact else f"{head}{base}.")

    @staticmethod
    def _say_fail(summary: dict) -> str:
        """Отчёт о провале: где именно и почему (без «что-то пошло не так»)."""
        failed = next((step for step in (summary.get("steps") or [])
                       if not step.get("ok")), None)
        reason = speakable(summary.get("reason", ""))
        if failed is None:
            return speakable(f"Не получилось: {reason or 'причина не сообщена'}")
        detail = reason or speakable(str(failed.get("reason", "")))
        return speakable(f"Не получилось на шаге {failed.get('index')} "
                         f"({failed.get('action')}): {detail or 'причина не сообщена'}")
