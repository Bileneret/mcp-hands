"""SQLite-память агента (этап 3, шаг 3): прогоны, шаги, вызовы LLM, инциденты.

Назначение — сделать провалы и цену работы наблюдаемыми (DESIGN §7): по каждой
задаче бенчмарка хранятся вердикт, время, токены, число перепланирований и
каждый проваленный шаг с причиной. Метрики считаются SQL-запросами — это
источник цифр для этапа бенчмарка (никакой ручной сборки из логов).

Правило «провалы не прячутся» на уровне схемы: у runs есть статус
`passed/failed/missing_tools`, у запущенных и упавших шагов — отдельные записи
в steps с причиной; инциденты (отказы стражей, не наступившие постусловия)
уходят в incidents и не теряются при замене плана перепланированием.

База одна на агента: `logs/agent_memory.db`. Файл — рабочий артефакт, в git не
попадает (`logs/` уже исключён).
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parents[1] / "logs" / "agent_memory.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',   -- running | passed | failed | missing_tools
    ok INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    started_ts REAL NOT NULL,
    ended_ts REAL,
    total_ms REAL,
    replans INTEGER NOT NULL DEFAULT 0,
    cumulative_tokens INTEGER NOT NULL DEFAULT 0,
    plan_source TEXT NOT NULL DEFAULT 'scripted'  -- scripted | llm
);
CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    idx INTEGER NOT NULL,
    action TEXT NOT NULL,
    ok INTEGER NOT NULL,
    attempts INTEGER NOT NULL,
    expect_kind TEXT NOT NULL,
    expect_ok INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    ignored_args TEXT NOT NULL DEFAULT '[]',
    ms REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES runs(id),
    task_id TEXT NOT NULL,
    model TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    ms REAL NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    verdict TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES runs(id),
    seen_ts REAL NOT NULL,
    kind TEXT NOT NULL,        -- guard_refused | expect_failed | transport | replan_stop
    action TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT ''
);
"""


@dataclass
class RunRecord:
    """Идентификатор прогона в памяти."""

    run_id: int


class Memory:
    """Обёртка над SQLite: тонкая, синхронная, без глобального состояния."""

    def __init__(self, db_path: Path | str = DEFAULT_DB) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # --- запись ------------------------------------------------------------

    def start_run(self, task_id: str, plan_source: str = "scripted") -> int:
        cursor = self.conn.execute(
            "INSERT INTO runs (task_id, status, started_ts, plan_source) VALUES (?,?,?,?)",
            (task_id, "running", time.time(), plan_source))
        self.conn.commit()
        return int(cursor.lastrowid)

    def record_step(self, run_id: int, outcome) -> None:
        """Пишет шаг как есть, включая провальные (провалы не прячутся)."""
        import json

        self.conn.execute(
            "INSERT INTO steps (run_id, idx, action, ok, attempts, expect_kind, expect_ok,"
            " reason, ignored_args, ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, outcome.index, outcome.action, int(outcome.ok), outcome.attempts,
             outcome.expect_kind, int(outcome.expect_ok), outcome.reason,
             json.dumps(outcome.ignored_args, ensure_ascii=False), outcome.ms))
        self.conn.commit()

    def record_incident(self, run_id: int | None, kind: str, action: str = "",
                        reason: str = "") -> None:
        self.conn.execute(
            "INSERT INTO incidents (run_id, seen_ts, kind, action, reason) VALUES (?,?,?,?,?)",
            (run_id, time.time(), kind, action, reason))
        self.conn.commit()

    def record_llm_call(self, *, task_id: str, model: str, attempt: int, ms: float = 0.0,
                        prompt_tokens: int = 0, completion_tokens: int = 0,
                        verdict: str = "", run_id: int | None = None) -> None:
        self.conn.execute(
            "INSERT INTO llm_calls (run_id, task_id, model, attempt, ms, prompt_tokens,"
            " completion_tokens, verdict) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, task_id, model, attempt, ms, prompt_tokens, completion_tokens, verdict))
        self.conn.commit()

    def finish_run(self, run_id: int, *, status: str, ok: bool, reason: str = "",
                   total_ms: float = 0.0, replans: int = 0,
                   cumulative_tokens: int = 0) -> None:
        self.conn.execute(
            "UPDATE runs SET status=?, ok=?, reason=?, ended_ts=?, total_ms=?, replans=?,"
            " cumulative_tokens=? WHERE id=?",
            (status, int(ok), reason, time.time(), total_ms, replans,
             cumulative_tokens, run_id))
        self.conn.commit()

    # --- чтение ------------------------------------------------------------

    def metrics(self) -> dict:
        """Сводка для отчёта бенчмарка (DESIGN §7 считается запросами)."""
        runs = [dict(row) for row in self.conn.execute(
            "SELECT task_id, status, COUNT(*) AS runs, SUM(ok) AS passed,"
            " AVG(total_ms) AS avg_ms, SUM(cumulative_tokens) AS tokens,"
            " SUM(replans) AS replans FROM runs GROUP BY task_id, status")]
        tokens_total = self.conn.execute(
            "SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS t,"
            " COUNT(*) AS calls FROM llm_calls").fetchone()
        incidents = [dict(row) for row in self.conn.execute(
            "SELECT kind, COUNT(*) AS n FROM incidents GROUP BY kind")]
        steps_failed = self.conn.execute(
            "SELECT COUNT(*) AS n FROM steps WHERE ok = 0").fetchone()["n"]
        return {"by_task_status": runs,
                "llm": {"calls": tokens_total["calls"], "tokens": tokens_total["t"],
                        "approx_usd": None},  # тариф провайдера меняется — цифра снаружи
                "incidents": incidents, "failed_steps": steps_failed}

    def close(self) -> None:
        self.conn.close()
