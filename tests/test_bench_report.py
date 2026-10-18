"""Тесты сводки метрик (этап 5): таблица из артефактов и подхват замеров владельца."""

from __future__ import annotations

import json

from mcp_hands.benchmark.report import build_summary


def _write_suite(bench_dir, name, mode, payload):
    data = {"mode": mode, **payload}
    (bench_dir / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_summary_builds_matrix_and_multiplier(tmp_path):
    bench = tmp_path / "bench"
    bench.mkdir()
    _write_suite(bench, "suite_a.json", "scripted (--dry-llm)", {
        "rounds": 3, "passed": 6, "failed": 0,
        "total_ms": 60_000.0,
        "per_task": {
            "b01": {"runs": 3, "passed": 3, "failed": 0, "missing_tools": 0,
                    "avg_ms": 5000.0, "min_ms": 4800.0, "max_ms": 5200.0},
            "b08": {"runs": 3, "passed": 3, "failed": 0, "missing_tools": 0,
                    "avg_ms": 300.0, "min_ms": 250.0, "max_ms": 350.0},
        }})
    _write_suite(bench, "suite_b.json", "llm", {
        "passed": 1, "failed": 0, "total_ms": 9_900.0,
        "per_task": {"b03": {"runs": 1, "passed": 1, "failed": 0, "missing_tools": 0,
                             "avg_ms": 4794.0, "min_ms": 4794.0, "max_ms": 4794.0}},
        "metrics": {"llm": {"calls": 2, "tokens": 1452}}})
    baseline = tmp_path / "HUMAN_BASELINE.md"
    baseline.write_text(
        "| Задача | Шаги | Время, мм:сс |\n|---|---|---|\n"
        "| b01 | ... | 1:45 |\n| b08 | ... | 0:20 |\n| b03 | ... | |\n",
        encoding="utf-8")
    db = tmp_path / "нет.db"  # базы нет — секция автономности просто пропускается

    out = build_summary(out_path=tmp_path / "summary.md", bench_dir=bench, db=db,
                        baseline=baseline)
    text = out.read_text(encoding="utf-8")
    assert "30" in text or "6/6" in text  # результаты серии
    assert "| b01 | 3/3 | 5.00 | 4.80 | 5.20 |" in text, text
    assert "GLM-5.3-Flash" in text and "1452" in text  # живой контур
    # человек: 1:45 + 0:20 = 125 с; агентский круг = 60 с / 3 круга = 20 с → ×6.2
    assert "125 с" in text and "×6.2" in text, text


def test_summary_without_measurements_is_honest(tmp_path):
    """Пока владелец не замерил — сводка честно пишет «ожидает замера», без оценок."""
    bench = tmp_path / "bench"
    bench.mkdir()
    _write_suite(bench, "suite_a.json", "scripted", {"rounds": 1, "passed": 0,
                                                     "failed": 0, "total_ms": 0,
                                                     "per_task": {}})
    baseline = tmp_path / "пусто.md"
    baseline.write_text("| Задача | Шаги | Время, мм:сс |\n|---|---|---|\n| b01 | ... | |\n",
                        encoding="utf-8")
    out = build_summary(out_path=tmp_path / "s.md", bench_dir=bench,
                        db=tmp_path / "нет.db", baseline=baseline)
    text = out.read_text(encoding="utf-8")
    assert "ожидает владельца" in text and "×" not in text.split("ожидает")[0][-20:]
