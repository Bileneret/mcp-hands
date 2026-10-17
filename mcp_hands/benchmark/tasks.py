"""Реестр задач бенчмарка (этап 3): десять задач из DESIGN §7.

Назначение — честный учёт готовности: каждая задача объявляет инструменты,
которыми она решается. Отсутствующие помечаются `missing_tools`, и задача при
прогоне complete с вердиктом «не покрыта набором» — это провал, который пишется,
а не имитация успеха (правило этапа 3: «запускаются end-to-end, провалы
фиксируются»). Формат записи фиксирован для этапа бенчмарка (метрики DESIGN §7).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..mcp_server import TOOL_NAMES


@dataclass(frozen=True)
class BenchmarkTask:
    """Одна задача бенчмарка: строки из DESIGN §7 + требования к инструментам."""

    id: str
    title: str
    needs: list[str] = field(default_factory=list)  # имена инструментов из набора mcp-hands
    note: str = ""


TASKS: list[BenchmarkTask] = [
    BenchmarkTask("b01", "Word: титульный лист + структура отчёта по шаблону",
                  needs=["word_report_from_template"],
                  note="требуется Word-COM сверх этапа 1"),
    BenchmarkTask("b02", "Word: форматирование документа (стили, шрифты, интервалы, оглавление)",
                  needs=["word_format_document"],
                  note="требуется Word-COM сверх этапа 1"),
    BenchmarkTask("b03", "Word → PDF: сохранение по шаблону имени", needs=["word_pdf_run"]),
    BenchmarkTask("b04", "Браузер: собрать источники по теме в таблицу",
                  needs=["browser_collect_sources"],
                  note="CDP сверх эталонного сценария ссылок"),
    BenchmarkTask("b05", "Браузер: текст статьи → Word с оформлением цитаты",
                  needs=["browser_extract_text", "word_report_from_template"]),
    BenchmarkTask("b06", "Браузер: разобрать сессию вкладок (сгруппировать, сохранить, закрыть лишнее)",
                  needs=["browser_tabs_manage"]),
    BenchmarkTask("b07", "YouTube: найти видео, субтитры/скорость, выписать таймкоды",
                  needs=["browser_youtube_capture"],
                  note="окно вне headless — риски фокуса"),
    BenchmarkTask("b08", "Файлы: разобрать папку загрузок по типам (удаление — только в корзину)",
                  needs=["fs_trash_move"],
                  note="корзина разрешена политикой, полное удаление — нет"),
    BenchmarkTask("b09", "Excel/CSV: свести данные из файлов + построить график",
                  needs=["excel_workbook_build"]),
    BenchmarkTask("b10", "Комбо-витрина: «отчёт по лабе» — поиск → таблица → оформление → PDF",
                  needs=["browser_extract_text", "word_report_from_template", "word_pdf_run"]),
]


def coverage() -> dict:
    """Готовность набора против каждой задачи: covered / missing_tools по именам."""
    rows = []
    for task in TASKS:
        missing = sorted(set(task.needs) - TOOL_NAMES)
        rows.append({"id": task.id, "title": task.title, "ready": not missing,
                     "missing_tools": missing, "note": task.note})
    return {"total": len(TASKS), "covered": sum(1 for r in rows if r["ready"]),
            "tasks": rows}
