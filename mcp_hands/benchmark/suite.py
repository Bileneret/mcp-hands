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

from ..brain.inspector import inspect_artifact
from ..brain.plan import parse_plan
from ..brain.runner import RunnerLimits, run_plan
from ..checkpoints import CheckpointStore
from ..memory import DEFAULT_DB, Memory
from .tasks import TASKS

ARTIFACTS = Path(__file__).resolve().parents[2] / "logs" / "benchmark"
CHECKPOINTS = Path(__file__).resolve().parents[2] / "logs" / "checkpoints"

# Скриптовые планы для покрытых задач (воспроизводимость без сети).
SCRIPTED_PLANS: dict[str, dict] = {
    "b03": {"task_id": "b03", "steps": [
        {"action": "word_pdf_run", "args": {},
         "expect": {"kind": "scenario_passed", "params": {"task": "b03"}}}]},
}


def _prepare_word_template(path: Path) -> None:
    """Готовит .docx-шаблон отчёта с плейсхолдерами (титул — литералами).

    Шаблон делается настоящим Word-ом через тот же COM-исполнитель: фикстурные
    бинарники в репозиторий не тащим, а живой документ честнее заготовки.
    """
    from ..office import WordSession
    from ..probe import DOMAIN_OPERATION_TIMEOUT_S, ProbeProcess

    title = ("МИНИСТЕРСТВО ОБРАЗОВАНИЯ\n"
             "ОТЧЁТ О ЛАБОРАТОРНОЙ РАБОТЕ\n\n")
    body = ("Тема: {{ТЕМА}}\n"
            "Автор: {{АВТОР}}\n"
            "Группа: {{ГРУППА}}\n"
            "Дата: {{ДАТА}}\n\n"
            "Введение\nЦель работы.\n\n"
            "Методика\nОписание.\n\n"
            "Результаты\nТаблица и вывод.\n")
    with ProbeProcess(default_timeout_s=DOMAIN_OPERATION_TIMEOUT_S) as satellite:
        word = WordSession(satellite)
        try:
            if not word.start().ok:
                raise RuntimeError("Word не поднялся для подготовки шаблона")
            created = word.new_document(title + body)
            if not created.ok:
                raise RuntimeError(f"шаблон не создан: {created.reason}")
            saved = word.save_as(str(path))
            if not (saved.ok and path.exists()):
                raise RuntimeError(f"шаблон не сохранён: {saved.reason}")
        finally:
            if word.pid:
                word.quit()


def _stand_report_from_template(workdir: Path) -> dict:
    """Стенд задачи b01: шаблон отчёта → замена реквизитов → готовый .docx."""
    template = workdir / "шаблон_отчёта.docx"
    report = workdir / "отчёт_лабы.docx"
    _prepare_word_template(template)
    plan = {"task_id": "b01", "steps": [
        {"action": "word_report_from_template",
         "args": {"template": str(template),
                  "replacements": {
                      "{{ТЕМА}}": "Разбор лабораторной работы",
                      "{{АВТОР}}": "MCP-Hands",
                      "{{ГРУППА}}": "ИУ-42",
                      "{{ДАТА}}": "18.10.2026"},
                  "save_as": str(report)},
         "expect": {"kind": "file_exists",
                    "params": {"path": str(report), "min_bytes": 2000}}}]}
    return {"plan": plan, "stand": str(workdir)}


def _stand_downloads(workdir: Path) -> dict:
    """Стенд задачи b08: папка-загрузки с известным набором файлов + план разбора.

    Стенды нужны там, где план зависит от подготовленного окружения (файлы,
    страницы-фикстуры): модель не должна угадывать пути — их даёт стенд.
    """
    source = workdir / "загрузки"
    target = workdir / "разобрано"
    source.mkdir(parents=True, exist_ok=True)
    (source / "отчёт_лабы.pdf").write_text("PDF-заглушка", encoding="utf-8")
    (source / "фото_стенда.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    (source / "архив_данных.zip").write_bytes(b"PK\x03\x04" + b"0" * 32)
    (source / "заметки.txt").write_text("заметки", encoding="utf-8")
    (source / "мусор_времянка.tmp").write_text("временный файл", encoding="utf-8")
    trash_path = target / "прочее" / "мусор_времянка.tmp"
    plan = {"task_id": "b08", "steps": [
        {"action": "fs_sort_by_type",
         "args": {"source": str(source), "target_root": str(target)},
         "expect": {"kind": "no_files_in", "params": {"dir": str(source)}}},
        {"action": "fs_trash_move",
         "args": {"paths": [str(trash_path)]},
         "expect": {"kind": "paths_gone", "params": {"paths": [str(trash_path)]}}}]}
    return {"plan": plan, "stand": str(workdir)}


def _prepare_word_draft(path: Path) -> None:
    """Черновик без оформления: заголовки — простым текстом (стили ставит план)."""
    body = ("ОГЛАВЛЕНИЕ\n\n"
            "Введение\nЦель работы и постановка задачи.\n\n"
            "Методика\nПоследовательность измерений и обработка.\n\n"
            "Результаты\nСводная таблица и выводы по работе.\n")
    from ..office import WordSession
    from ..probe import DOMAIN_OPERATION_TIMEOUT_S, ProbeProcess

    with ProbeProcess(default_timeout_s=DOMAIN_OPERATION_TIMEOUT_S) as satellite:
        word = WordSession(satellite)
        try:
            if not word.start().ok:
                raise RuntimeError("Word не поднялся для черновика")
            if not word.new_document(body).ok:
                raise RuntimeError("черновик не создан")
            saved = word.save_as(str(path))
            if not (saved.ok and path.exists()):
                raise RuntimeError("черновик не сохранён")
        finally:
            if word.pid:
                word.quit()


def _stand_format_document(workdir: Path) -> dict:
    """Стенд задачи b02: черновик → стили/шрифт/интервалы/оглавление → готовый .docx."""
    draft = workdir / "черновик.docx"
    styled = workdir / "оформленный.docx"
    _prepare_word_draft(draft)
    plan = {"task_id": "b02", "steps": [
        {"action": "word_format_document",
         "args": {"path": str(draft),
                  "save_as": str(styled),
                  "actions": [
                      {"op": "set_style", "find": "Введение", "style": "Heading 1"},
                      {"op": "set_style", "find": "Методика", "style": "Heading 1"},
                      {"op": "set_style", "find": "Результаты", "style": "Heading 1"},
                      {"op": "set_font", "find": "Введение", "name": "Times New Roman",
                       "size": 14},
                      {"op": "set_spacing", "line_spacing": 1.5, "space_after": 6},
                      {"op": "insert_toc", "find": "ОГЛАВЛЕНИЕ"}]},
         "expect": {"kind": "file_exists",
                    "params": {"path": str(styled), "min_bytes": 2000}}}]}
    return {"plan": plan, "stand": str(workdir)}


def _stand_excel_merge(workdir: Path) -> dict:
    """Стенд задачи b09: два CSV с одинаковыми колонками → сведённая книга с графиком."""
    first = workdir / "данные_январь_февраль.csv"
    second = workdir / "данные_март_апрель.csv"
    first.write_text("Месяц,Выручка\nЯнварь,120\nФевраль,150\n", encoding="utf-8")
    second.write_text("Месяц,Выручка\nМарт,170\nАпрель,140\n", encoding="utf-8")
    merged = workdir / "сводная_выручка.xlsx"
    plan = {"task_id": "b09", "steps": [
        {"action": "excel_workbook_build",
         "args": {"inputs": [str(first), str(second)],
                  "output": str(merged),
                  "sheet_name": "Свод",
                  "chart": {"type": "column", "title": "Выручка по месяцам"}},
         "expect": {"kind": "file_exists",
                    "params": {"path": str(merged), "min_bytes": 5000}}}]}
    return {"plan": plan, "stand": str(workdir)}


def _stand_tabs_session(workdir: Path) -> dict:
    """Стенд задачи b06: три страницы-файла → сессия вкладок с дубликатом → разбор.

    Вкладки: A (дважды), B — постусловие инструмента: остаются ровно keep (A, B).
    """
    page_a = workdir / "страница_A.html"
    page_b = workdir / "страница_B.html"
    page_a.write_text("<html><head><title>Alpha</title></head><body>A</body></html>",
                      encoding="utf-8")
    page_b.write_text("<html><head><title>Beta</title></head><body>B</body></html>",
                      encoding="utf-8")
    url_a = page_a.resolve().as_uri()
    url_b = page_b.resolve().as_uri()
    report = workdir / "отчёт_вкладок.json"
    plan = {"task_id": "b06", "steps": [
        {"action": "browser_tabs_audit",
         "args": {"urls": [url_a, url_a, url_b, url_a], "output": str(report),
                  "keep": [url_a, url_b]},
         "expect": {"kind": "file_exists", "params": {"path": str(report)}}}]}
    return {"plan": plan, "stand": str(workdir)}


def _stand_sources_page(workdir: Path) -> dict:
    """Стенд задачи b04: страница с четырьмя карточками источников (article.source)."""
    cards = [
        ("Иванов И. И.", "2019", "https://example.org/alpha", "Альфа-исследование"),
        ("Петров П. П.", "2021", "https://example.org/beta", "Бета-обзор"),
        ("Сидоров С. С.", "2018", "https://example.org/gamma", "Гамма-метод"),
        ("Кузнецова К. К.", "2022", "https://example.org/delta", "Дельта-анализ"),
    ]
    items = "\n".join(
        f'<article class="source" data-year="{year}">'
        f'<a href="{url}">{title}</a><span class="author">{author}</span></article>'
        for author, year, url, title in cards)
    page = workdir / "источники.html"
    page.write_text(f"<html><head><title>Стенд источников</title></head>"
                    f"<body><main>{items}</main></body></html>", encoding="utf-8")
    csv_out = workdir / "источники.csv"
    plan = {"task_id": "b04", "steps": [
        {"action": "browser_collect_sources",
         "args": {"url": page.resolve().as_uri(), "output_csv": str(csv_out)},
         "expect": {"kind": "file_contains",
                    "params": {"path": str(csv_out), "needle": "Иванов И. И."}}}]}
    return {"plan": plan, "stand": str(workdir)}


ARTICLE_PAGE = ("<html><head><title>Статья стенда</title></head><body><article>"
                "<h1>Метод локальной автоматизации интерфейсов</h1>"
                "<p>Введение: задача автоматизации рутинных операций на рабочем столе.</p>"
                "<p>Метод: адресные запросы к дереву доступности вместо обхода пикселей.</p>"
                "<p>Результат: устойчивое выполнение сценариев без вмешательства.</p>"
                "</article></body></html>")


def _stand_extract_article(workdir: Path) -> dict:
    """Стенд задачи b05: статья-страница → документ Word с оформлением цитаты."""
    page = workdir / "статья.html"
    page.write_text(ARTICLE_PAGE, encoding="utf-8")
    docx = workdir / "статья_с_цитатой.docx"
    plan = {"task_id": "b05", "steps": [
        {"action": "browser_extract_text",
         "args": {"url": page.resolve().as_uri(), "output_docx": str(docx)},
         "expect": {"kind": "file_exists", "params": {"path": str(docx), "min_bytes": 2000}}}]}
    return {"plan": plan, "stand": str(workdir)}


def _stand_lab_report(workdir: Path) -> dict:
    """Стенд задачи b10 (комбо): источники → CSV, статья → Word, Word → PDF."""
    sources = _stand_sources_page(workdir)
    article = workdir / "статья.html"
    article.write_text(ARTICLE_PAGE, encoding="utf-8")
    article_url = article.resolve().as_uri()
    csv_out = workdir / "источники.csv"
    docx = workdir / "доклад.docx"
    pdf = workdir / "доклад.pdf"
    plan = {"task_id": "b10", "steps": [
        {"action": "browser_collect_sources",
         "args": {"url": sources["plan"]["steps"][0]["args"]["url"],
                  "output_csv": str(csv_out)},
         "expect": {"kind": "file_contains",
                    "params": {"path": str(csv_out), "needle": "Иванов И. И."}}},
        {"action": "browser_extract_text",
         "args": {"url": article_url, "output_docx": str(docx)},
         "expect": {"kind": "file_exists", "params": {"path": str(docx), "min_bytes": 2000}}},
        {"action": "word_docx_to_pdf",
         "args": {"path": str(docx), "output_pdf": str(pdf)},
         "expect": {"kind": "file_exists", "params": {"path": str(pdf), "min_bytes": 2000}}}]}
    return {"plan": plan, "stand": str(workdir)}


VIDEO_PAGE = ("<html><head><title>Стенд медиа</title></head><body>"
              "<h1>Практика автоматизации: разбор записи</h1>"
              '<video id="player" data-duration="03:20"></video>'
              '<div id="transcript">'
              '<span class="cue" data-start="00:00" data-end="00:24">Введение: цель записи.</span>'
              '<span class="cue" data-start="00:24" data-end="01:02">Обзор инструмента.</span>'
              '<span class="cue" data-start="01:02" data-end="02:10">Разбор методики.</span>'
              '<span class="cue" data-start="02:10" data-end="03:20">Выводы и итоги.</span>'
              "</div></body></html>")


def _stand_video_notes(workdir: Path) -> dict:
    """Стенд задачи b07: локальная медиа-страница (плеер + реплики) → заметки
    с таймкодами и выбранной скоростью. Живой YouTube сознательно не используется."""
    page = workdir / "видео.html"
    page.write_text(VIDEO_PAGE, encoding="utf-8")
    report_json = workdir / "видео_отчёт.json"
    notes = workdir / "заметки_по_видео.md"
    plan = {"task_id": "b07", "steps": [
        {"action": "browser_video_capture",
         "args": {"url": page.resolve().as_uri(), "output_json": str(report_json),
                  "output_notes": str(notes), "playback_rate": 1.5},
         "expect": {"kind": "file_contains",
                    "params": {"path": str(notes), "needle": "[02:10–03:20]"}}}]}
    return {"plan": plan, "stand": str(workdir)}


STANDS: dict[str, object] = {"b08": _stand_downloads,
                             "b01": _stand_report_from_template,
                             "b02": _stand_format_document,
                             "b09": _stand_excel_merge,
                             "b06": _stand_tabs_session,
                             "b04": _stand_sources_page,
                             "b05": _stand_extract_article,
                             "b10": _stand_lab_report,
                             "b07": _stand_video_notes}


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
              limits: RunnerLimits | None = None, rounds: int = 1,
              hardening: bool = True) -> dict:
    """Прогоняет выбранные задачи (по умолчанию все 10) и пишет метрики в память.

    `rounds` — повторы задачи (приёмка этапа 4: ≥3 кругов для порога автономности).
    `hardening` — включить контур этапа 4: L2-инспектор (`verifier`) и чекпойнты.
    Сводка несёт `autonomy` (прогоны без вмешательств человека — DESIGN §7).
    """
    memory = Memory(db_path)
    started = time.perf_counter()
    rows: list[dict] = []
    verifier = inspect_artifact if hardening else None
    checkpoints = CheckpointStore(ARTIFACTS.parent / "checkpoints") if hardening else None
    config = None
    if live:
        from ..brain.llm import load_config

        config = load_config()
    try:
        for round_index in range(1, max(1, rounds) + 1):
          for task in TASKS:
            if task_ids and task.id not in task_ids:
                continue
            if task.id in STANDS:
                stamp = time.strftime("%Y%m%d_%H%M%S")
                stand_workdir = ARTIFACTS / "stands" / task.id / f"{stamp}_r{round_index}"
                stand_workdir.mkdir(parents=True, exist_ok=True)
                prepared = STANDS[task.id](stand_workdir)
                plan = parse_plan(prepared["plan"])
                run = run_plan(plan, limits or RunnerLimits(), memory=memory,
                               plan_source="scripted-stand", verifier=verifier,
                               checkpoints=checkpoints)
                rows.append({"task_id": task.id, "round": round_index,
                             "status": "passed" if run.ok else "failed",
                             "reason": run.reason, "total_ms": round(run.total_ms, 1),
                             "replans": run.replans, "steps": len(run.steps),
                             "stand": prepared["stand"]})
                continue
            if task.id not in SCRIPTED_PLANS:
                # задача не покрыта набором инструментов — честная фиксация
                rows.append({**_record_missing(task, memory), "round": round_index})
                continue
            plan_dict = SCRIPTED_PLANS[task.id]
            if live:
                from ..brain.llm import request_plan

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
                    rows.append({"task_id": task.id, "round": round_index,
                                 "status": "failed", "reason": result.error})
                    continue
                plan = result.plan
                source = "llm"
            else:
                plan = parse_plan(plan_dict)
                source = "scripted"
            run = run_plan(plan, limits or RunnerLimits(), memory=memory,
                           plan_source=source, verifier=verifier,
                           checkpoints=checkpoints,
                           llm_request_plan=(
                               _live_replan(config, memory, task) if live else None))
            rows.append({"task_id": task.id, "round": round_index,
                         "status": "passed" if run.ok else "failed",
                         "reason": run.reason, "total_ms": round(run.total_ms, 1),
                         "replans": run.replans, "steps": len(run.steps)})
    finally:
        summary = {"mode": "llm" if live else "scripted (--dry-llm)",
                   "rounds": max(1, rounds),
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
    rounds = 1
    if "--tasks" in argv:
        index = argv.index("--tasks")
        task_ids = [t.strip() for t in argv[index + 1].split(",") if t.strip()]
    if "--rounds" in argv:
        rounds = int(argv[argv.index("--rounds") + 1])
    no_hardening = "--no-hardening" in argv
    summary = run_suite(task_ids=task_ids, live=live, rounds=rounds,
                        hardening=not no_hardening)
    print(json.dumps({k: summary[k] for k in
                      ("mode", "rounds", "passed", "missing_tools", "failed",
                       "total_ms", "report")},
                     ensure_ascii=False, indent=1))
    print("автономность:", json.dumps(summary["metrics"]["autonomy"], ensure_ascii=False))
    for row in summary["tasks"]:
        mark = {"passed": "+", "missing_tools": "~", "failed": "!"}[row["status"]]
        print(f"  [{mark}] {row['task_id']}: {row['status']} {row.get('reason', '')[:80]}")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
