"""Тесты чекпойнтов (этап 4, шаг 2): резерв → изменение → откат; лимиты; runner."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_hands.brain import runner as runner_mod
from mcp_hands.brain.plan import parse_plan
from mcp_hands.brain.runner import RunnerLimits, run_plan
from mcp_hands.checkpoints import CheckpointStore
from mcp_hands.mcp_server import ToolResult


def test_create_rollback_restores_content_and_removes_newcomer(tmp_path):
    store = CheckpointStore(tmp_path / "checkpoints")
    original = tmp_path / "отчёт.docx"
    original.write_text("ДОСТОЙНЫЙ КОНТЕНТ", encoding="utf-8")
    checkpoint = store.create("run", 1, [str(original)])
    assert checkpoint.ok and len(checkpoint.entries) == 1

    original.write_text("ЗАТИРАНИЕ", encoding="utf-8")   # «плохой» шаг испортил файл
    newcomer = tmp_path / "мусор.tmp"
    newcomer.write_text("появился", encoding="utf-8")
    checkpoint.entries[str(newcomer)] = {"absent": True, "sha256": "", "bytes": 0}

    data = store.rollback(checkpoint)
    assert data["ok"], data
    assert original.read_text(encoding="utf-8") == "ДОСТОЙНЫЙ КОНТЕНТ", "контент восстановлен"
    assert str(newcomer) in data["removed_appeared"] and not newcomer.exists()


def test_rollback_reports_lost_backup(tmp_path):
    store = CheckpointStore(tmp_path / "checkpoints")
    original = tmp_path / "файл.txt"
    original.write_text("оригинал", encoding="utf-8")
    checkpoint = store.create("run", 1, [str(original)])
    backup_path = Path(checkpoint.entries[str(original)]["backup"])
    backup_path.unlink()  # бэкап потерян
    data = store.rollback(checkpoint)
    assert not data["ok"] and data["missing_backups"], data
    assert original.read_text(encoding="utf-8") == "оригинал"  # исходник не заменён


def test_rollback_with_nothing_to_restore_is_ok(tmp_path):
    store = CheckpointStore(tmp_path / "checkpoints")
    checkpoint = store.create("run", 1, [])
    assert checkpoint.ok and checkpoint.entries == {}
    data = store.rollback(checkpoint)
    assert data["ok"] and data["restored"] == [], "пустой откат — не ошибка"


def test_eviction_keeps_total_under_limit(tmp_path):
    store = CheckpointStore(tmp_path / "checkpoints", max_total_bytes=256 * 1024)
    for index in range(6):
        payload = tmp_path / f"данные_{index}.bin"
        payload.write_bytes(b"0" * 120 * 1024)  # 120 КБ каждый
        store.create("run", index, [str(payload)])
    total = sum(f.stat().st_size for f in (tmp_path / "checkpoints").rglob("*") if f.is_file())
    assert total <= 256 * 1024 * 2 + 120 * 1024, f"переполнение: {total} байт"


def test_runner_snapshots_and_rolls_back_failed_mutating_step(tmp_path):
    """Неуспех изменяющего шага → откат: файл возвращён, вердикт failed честный."""
    target = tmp_path / "документ.docx"
    target.write_text("ЦЕННОЕ СОДЕРЖИМОЕ", encoding="utf-8")
    store = CheckpointStore(tmp_path / "checkpoints")

    def bad_tool(**kwargs):  # как будто Word испортил файл и шаг упал
        target.write_text("ИСПОРЧЕННЫЙ КОПИРАЙТИНГ", encoding="utf-8")
        return ToolResult(ok=False, reason="Word закрылся в середине шага")

    original_tool = runner_mod.DISPATCH_TABLE["word_report_from_template"]
    runner_mod.DISPATCH_TABLE["word_report_from_template"] = bad_tool
    try:
        plan = parse_plan({"task_id": "x", "steps": [
            {"action": "word_report_from_template",
             "args": {"template": "t", "replacements": {"a": "b"}, "save_as": str(target)},
             "expect": {"kind": "file_exists", "params": {"path": str(target)}}}]})
        run = run_plan(plan, RunnerLimits(attempts_per_step=1), checkpoints=store)
    finally:
        runner_mod.DISPATCH_TABLE["word_report_from_template"] = original_tool

    assert not run.ok and run.steps[0].rolled_back, run.summary()
    assert target.read_text(encoding="utf-8") == "ЦЕННОЕ СОДЕРЖИМОЕ", "откат не восстановил"


def test_runner_keeps_file_when_step_succeeds(tmp_path):
    """Успешный шаг: чекпойнт создан, но файл остаётся тем, что вернул инструмент."""
    target = tmp_path / "выход.docx"
    store = CheckpointStore(tmp_path / "checkpoints")

    def good_tool(**kwargs):
        Path(kwargs["save_as"]).write_text("НОВЫЙ ДОКУМЕНТ", encoding="utf-8")
        return ToolResult(ok=True, summary="ок", detail={"saved": kwargs["save_as"]})

    original_tool = runner_mod.DISPATCH_TABLE["word_report_from_template"]
    runner_mod.DISPATCH_TABLE["word_report_from_template"] = good_tool
    try:
        plan = parse_plan({"task_id": "x", "steps": [
            {"action": "word_report_from_template",
             "args": {"template": "t", "replacements": {"a": "b"}, "save_as": str(target)},
             "expect": {"kind": "file_exists", "params": {"path": str(target)}}}]})
        run = run_plan(plan, RunnerLimits(attempts_per_step=1), checkpoints=store)
    finally:
        runner_mod.DISPATCH_TABLE["word_report_from_template"] = original_tool
    assert run.ok and not run.steps[0].rolled_back, run.summary()
    assert target.read_text(encoding="utf-8") == "НОВЫЙ ДОКУМЕНТ"
