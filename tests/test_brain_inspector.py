"""Тесты L2-инспектора (этап 4, шаг 1): арбитраж артефактов без COM.

docx/xlsx собираются как ZIP+XML руками — инспектор по построению не зависит
ни от Office, ни от наших обёрток. Поведение runner при расхождении — disputed.
"""

from __future__ import annotations

import zipfile

import pytest

from mcp_hands.brain import runner as runner_mod
from mcp_hands.brain.inspector import InspectError, docx_text, inspect_artifact, sha256, xlsx_probe
from mcp_hands.brain.plan import parse_plan
from mcp_hands.brain.runner import RunnerLimits, run_plan
from mcp_hands.mcp_server import ToolResult


def make_docx(path, text: str, charts: int = 0) -> None:
    document = (f'<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org'
                f'/wordprocessingml/2006/main"><w:body>' +
                "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in text.split("\n")) +
                "</w:body></w:document>")
    entries = {"[Content_Types].xml": "<Types/>", "word/document.xml": document}
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)


def make_xlsx(path, cells: dict[str, str], shared: list[str], charts: int = 0) -> None:
    shared_xml = ('<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org'
                  '/spreadsheetml/2006/main">' +
                  "".join(f"<si><t>{item}</t></si>" for item in shared) + "</sst>")
    cell_xml = "".join(
        f'<c r="{address}" t="s"><v>{shared.index(value)}</v></c>' for address, value in cells.items())
    sheet_xml = ('<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org'
                 '/spreadsheetml/2006/main"><sheetData>' + cell_xml + "</sheetData></worksheet>")
    entries = {"[Content_Types].xml": "<Types/>", "xl/sharedStrings.xml": shared_xml,
               "xl/worksheets/sheet1.xml": sheet_xml}
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)


def test_inspector_docx_text_and_missing(tmp_path):
    docx = tmp_path / "отчёт.docx"
    make_docx(docx, "Титульный лист\nТема: разбор")
    assert "Титульный лист" in docx_text(docx)
    empty = tmp_path / "пустой.docx"
    make_docx(empty, "")
    report = inspect_artifact(empty)
    assert report["kind"] == "docx" and not report["text_sample"].strip()
    assert len(report["sha256"]) == 64


def test_inspector_xlsx_cells_and_charts(tmp_path):
    xlsx = tmp_path / "свод.xlsx"
    make_xlsx(xlsx, {"A1": "Месяц", "B1": "Выручка", "A2": "Январь"}, ["Месяц", "Выручка", "Январь"])
    probe = xlsx_probe(xlsx)
    assert probe["cells"]["A1"] == "Месяц" and probe["cells"]["A2"] == "Январь"
    assert probe["charts"] == 0
    report = inspect_artifact(xlsx)
    assert report["kind"] == "xlsx" and report["charts"] == 0


def test_inspector_error_on_garbage(tmp_path):
    garbage = tmp_path / "подделка.docx"
    garbage.write_text("это не zip", encoding="utf-8")
    with pytest.raises(InspectError):
        inspect_artifact(garbage)


# --- интеграция: disputed в runner --------------------------------------------------


def _fake_tool_result(detail: dict) -> ToolResult:
    return ToolResult(ok=True, summary="ок", detail=detail)


def test_runner_skips_l2_when_no_artifact(tmp_path):
    """У шага нет поддерживаемого артефакта (health) — L2 неприменим, шаг ok."""
    plan = parse_plan({"task_id": "x", "steps": [
        {"action": "health", "args": {},
         "expect": {"kind": "file_contains",
                    "params": {"path": str(tmp_path / "f.txt"), "needle": "z"}}}]})
    (tmp_path / "f.txt").write_text("zh", encoding="utf-8")

    def verifier(_path):
        raise AssertionError("инспектор не должен вызываться без артефакта")

    run = run_plan(plan, RunnerLimits(attempts_per_step=1), verifier=verifier)
    assert run.ok


def test_runner_disputes_when_l2_sees_empty_document(tmp_path):
    """L1 ok, а независимый парсер видит пустой документ → шаг disputed,
    прогон не считается успешным, инцидент l2_disputed записан в память."""
    docx = tmp_path / "пустышка.docx"
    make_docx(docx, "")
    plan = parse_plan({"task_id": "x", "steps": [
        {"action": "word_report_from_template",
         "args": {"template": "t", "replacements": {"a": "b"}, "save_as": "s"},
         "expect": {"kind": "file_exists", "params": {"path": str(docx)}}}]})
    fake = _fake_tool_result({"saved": str(docx), "template": "t"})
    original = runner_mod.DISPATCH_TABLE["word_report_from_template"]
    runner_mod.DISPATCH_TABLE["word_report_from_template"] = lambda **kwargs: fake
    try:
        run = run_plan(plan, RunnerLimits(attempts_per_step=1),
                       verifier=inspect_artifact)
    finally:
        runner_mod.DISPATCH_TABLE["word_report_from_template"] = original
    step = run.steps[0]
    assert step.disputed and not run.ok, step
    assert "L2 расхождение" in step.reason


def test_runner_confirms_when_l2_agrees(tmp_path):
    """L2 подтверждает непустой документ: шаг ok, disputed нет."""
    docx = tmp_path / "наполненный.docx"
    make_docx(docx, "Титульный лист\nСодержимое отчёта")
    plan = parse_plan({"task_id": "x", "steps": [
        {"action": "word_report_from_template",
         "args": {"template": "t", "replacements": {"a": "b"}, "save_as": "s"},
         "expect": {"kind": "file_exists", "params": {"path": str(docx)}}}]})
    fake = _fake_tool_result({"saved": str(docx)})
    original = runner_mod.DISPATCH_TABLE["word_report_from_template"]
    runner_mod.DISPATCH_TABLE["word_report_from_template"] = lambda **kwargs: fake
    try:
        run = run_plan(plan, RunnerLimits(attempts_per_step=1),
                       verifier=inspect_artifact)
    finally:
        runner_mod.DISPATCH_TABLE["word_report_from_template"] = original
    assert run.ok and not run.steps[0].disputed, run.summary()
    assert "L2:" in run.steps[0].evidence
