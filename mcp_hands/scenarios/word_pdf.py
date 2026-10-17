"""Сценарий «Word → PDF»: офисный путь через COM-исполнитель.

Инжекции ввода здесь нет вовсе: всё делается через COM в процессе-сателлите. Постусловия
проверяются по фактам — файл на диске и **текст готового PDF**, а не по коду возврата
Word. Так ошибка в замене плейсхолдеров не сможет остаться незамеченной.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from ..office import WORD_IMAGE, WordSession
from ..probe import DOMAIN_OPERATION_TIMEOUT_S, ProbeProcess
from .runner import Run

PLACEHOLDERS = {
    "{{ТЕМА}}": "Сценарий Word → PDF",
    "{{АВТОР}}": "MCP-Hands",
    "{{ДАТА}}": "17.10.2026",
}
BODY = ("Тема: {{ТЕМА}}\n"
        "Автор: {{АВТОР}}\n"
        "Дата: {{ДАТА}}\n"
        "Проверка офисного пути: замена и экспорт.\n")


def pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def scenario(run: Run, workdir: Path) -> None:
    template = workdir / "шаблон.docx"
    pdf = workdir / "отчёт.pdf"

    with ProbeProcess(default_timeout_s=DOMAIN_OPERATION_TIMEOUT_S) as satellite:
        word = WordSession(satellite)
        try:
            with run.step("поднять Word в сателлите", expect="pid известен и взят под опеку",
                          method="COM: DispatchEx + регистрация pid до блокирующих вызовов") as step:
                outcome = word.start()
                step.details["outcome"] = outcome.describe()
                identity = ProbeProcess.process_identity(word.pid) if word.pid else {}
                step.details["identity"] = identity
                ok = outcome.ok and identity.get("image", "").lower() == WORD_IMAGE.lower()
                run.confirm(f"Word pid {word.pid}, образ {identity.get('image')!r}, "
                            f"в реестре: {bool(outcome.details.get('registry'))}", ok=ok)
                if not ok:
                    step.error = outcome.reason or "Word не поднялся"
                    return

            with run.step("создать документ по шаблону", expect="документ создан с текстом",
                          method="COM: Documents.Add + Content.Text") as step:
                created = word.new_document(BODY)
                step.details["outcome"] = created.describe()
                run.confirm(f"документ {created.value.get('name')!r}, "
                            f"{created.value.get('characters')} знаков", ok=created.ok)
                if not created.ok:
                    step.error = created.reason

            with run.step("сохранить шаблон", expect="файл шаблона на диске",
                          method="COM: SaveAs2 с абсолютным путём") as step:
                saved = word.save_as(str(template))
                step.details["outcome"] = saved.describe()
                exists = template.exists()
                step.details["file"] = {"path": str(template), "bytes": template.stat().st_size
                                        if exists else 0}
                run.confirm(f"шаблон {template.stat().st_size if exists else 0} байт",
                            ok=saved.ok and exists)
                if not (saved.ok and exists):
                    step.error = saved.reason or "файл шаблона не появился"

            with run.step("заменить плейсхолдеры", expect="в документе не осталось плейсхолдеров",
                          method="COM: Find.Execute позиционно + проверка состояния документа") as step:
                replaced = word.replace(PLACEHOLDERS)
                step.details["outcome"] = replaced.describe()
                run.confirm(f"осталось плейсхолдеров: {len(replaced.value.get('remaining', []))}",
                            ok=replaced.ok and replaced.value.get("replaced_all"))
                if not (replaced.ok and replaced.value.get("replaced_all")):
                    step.error = replaced.reason or f"остались {replaced.value.get('remaining')}"

            with run.step("экспортировать в PDF", expect="PDF создан",
                          method="COM: ExportAsFixedFormat (17 = PDF)") as step:
                exported = word.export_pdf(str(pdf))
                step.details["outcome"] = exported.describe()
                exists = pdf.exists()
                step.details["file"] = {"path": str(pdf), "bytes": pdf.stat().st_size if exists else 0}
                run.confirm(f"PDF {pdf.stat().st_size if exists else 0} байт", ok=exported.ok and exists)
                if not (exported.ok and exists):
                    step.error = exported.reason or "PDF не создан"

            with run.step("проверить текст PDF", expect="значения на месте, плейсхолдеров нет",
                          method="pypdf: извлечение текста и сверка") as step:
                try:
                    text = pdf_text(pdf)
                except Exception as exc:  # noqa: BLE001 — битый PDF это результат шага
                    step.error = f"не удалось прочитать PDF: {exc}"
                    return
                missing = [value for value in PLACEHOLDERS.values() if value not in text]
                leftover = [key for key in PLACEHOLDERS if key in text]
                step.details["text"] = text[:200]
                run.confirm(f"знаков в PDF {len(text)}, не найдено значений: {len(missing)}, "
                            f"остатки плейсхолдеров: {len(leftover)}",
                            ok=not missing and not leftover)
                if missing or leftover:
                    step.error = f"нет значений {missing}, остатки {leftover}"

            with run.step("закрыть Word", expect="процесс завершён, запись снята с учёта",
                          method="COM: Quit + проверка исчезновения процесса") as step:
                pid = word.pid
                outcome = word.quit()
                step.details["outcome"] = outcome.describe()
                gone = not ProbeProcess.process_identity(pid)["image"] if pid else True
                step.details.update({"pid": pid, "process_gone": gone})
                run.confirm(f"процесс {pid} завершён: {gone}", ok=outcome.ok and gone)
                if not (outcome.ok and gone):
                    step.error = outcome.reason or f"процесс {pid} остался висеть"
        finally:
            # Убираем ТОЛЬКО свой экземпляр Word: у пользователя может быть открыт
            # его собственный документ, и трогать его нельзя ни при каких условиях.
            if word.pid:
                word.quit()
                time.sleep(0.5)
