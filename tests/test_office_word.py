"""Живые проверки COM-исполнителя Word (шаг 4 этапа 1).

Здесь проверяется то, ради чего весь механизм: Word запускается в процессе-сателлите,
его pid встаёт под опеку ДО первой блокирующей операции, документ проходит путь
шаблон → замена плейсхолдеров → PDF, а после штатного завершения процесс исчезает
и запись из реестра снимается.

Тесты помечены `office`: Word поднимается медленно (секунды), поэтому модуль целиком
можно исключить из быстрого прогона — `pytest -m "not office"`.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from mcp_hands.events import EventPump
from mcp_hands.office import WORD_IMAGE, ModalWatcher, WordSession
from mcp_hands.probe import DOMAIN_OPERATION_TIMEOUT_S, ProbeProcess

pytestmark = pytest.mark.office

PLACEHOLDERS = {"{{ТЕМА}}": "Проверка COM-исполнителя",
                "{{АВТОР}}": "MCP-Hands",
                "{{ДАТА}}": "17.10.2026"}


@pytest.fixture(scope="module")
def session():
    """Один запуск Word на весь модуль: каждый старт стоит секунды."""
    with ProbeProcess(default_timeout_s=DOMAIN_OPERATION_TIMEOUT_S) as satellite:
        word = WordSession(satellite)
        outcome = word.start()
        if not outcome.ok:
            # Прибираемся ДО пропуска: pytest не выполняет код после `yield`, когда
            # фикстура пропускает тест, и запущенный Word остался бы висеть.
            word.quit()
            pytest.skip(f"Word недоступен: {outcome.reason}")
        try:
            yield word
        finally:
            word.quit()


def test_word_started_and_tracked(session):
    """Word поднят, его pid известен и записан в файл-реестр сателлита."""
    assert session.pid > 0, "pid процесса Word не получен"
    identity = ProbeProcess.process_identity(session.pid)
    assert identity["image"].lower() == WORD_IMAGE.lower(), \
        f"процесс под опекой — не Word: {identity}"
    assert identity["created"], "время создания процесса не получено"
    entries = json.loads(session.probe.pid_file.read_text(encoding="utf-8"))
    assert any(entry["pid"] == session.pid for entry in entries), \
        f"pid Word не попал в файл-реестр: {entries}"
    tracked = [item for item in session.probe.tracked_processes() if item["pid"] == session.pid]
    assert tracked and tracked[0]["image"].lower() == WORD_IMAGE.lower()


def test_word_status_reports_documents(session):
    status = session.status()
    assert status.ok, status.reason
    assert status.value["running"] is True
    assert status.value["pid"] == session.pid


def test_template_path_to_pdf(session, tmp_path: Path):
    """Полный проход: шаблон с плейсхолдерами → замена → PDF с проверкой текста.

    Проверяется содержимое готового PDF, а не факт его существования: иначе ошибка
    в замене плейсхолдеров осталась бы незамеченной.
    """
    template = tmp_path / "шаблон.docx"
    document_text = "Тема: {{ТЕМА}}\nАвтор: {{АВТОР}}\nДата: {{ДАТА}}\n"

    created = session.new_document(document_text)
    assert created.ok, created.reason

    saved = session.save_as(str(template))
    assert saved.ok and saved.value["exists"], f"шаблон не сохранён: {saved.describe()}"

    replaced = session.replace(PLACEHOLDERS)
    assert replaced.ok, replaced.reason
    assert replaced.value["replaced_all"], \
        f"в документе остались плейсхолдеры: {replaced.value['remaining']}"

    pdf = tmp_path / "отчёт.pdf"
    exported = session.export_pdf(str(pdf))
    assert exported.ok, exported.reason
    assert exported.value["exists"] and exported.value["bytes"] > 1000, \
        f"PDF подозрительно мал: {exported.value}"

    text = _pdf_text(pdf)
    for value in PLACEHOLDERS.values():
        assert value in text, f"в PDF нет значения {value!r}: {text[:200]!r}"
    assert "{{" not in text, "в PDF остались незаменённые плейсхолдеры"


def test_modal_watcher_detects_nothing_on_clean_run(session):
    """На штатной работе модальных окон не появляется — сторож молчит."""
    with EventPump() as pump:
        watcher = ModalWatcher(pump)
        session.modal_watcher = watcher
        try:
            assert session.status().ok
            assert watcher.recent(session.pid) == "", "сторож нашёл модальное окно на ровном месте"
        finally:
            session.modal_watcher = None


def test_word_quit_removes_registry_entry(session):
    """Штатное завершение: процесс исчезает, запись из реестра снимается.

    Проверяется в конце модуля: после этого Word должен быть недоступен, а файл-реестр
    не должен содержать его pid — иначе при следующем аварийном снятии супервизор
    попытался бы убить несуществующий процесс.
    """
    pid = session.pid
    outcome = session.quit()
    assert outcome.ok, outcome.reason
    assert session.pid == 0
    deadline = time.time() + 20
    while time.time() < deadline:
        if not ProbeProcess.process_identity(pid)["image"]:
            break
        time.sleep(0.5)
    assert not ProbeProcess.process_identity(pid)["image"], f"процесс {pid} остался висеть"
    entries_path = session.probe.pid_file
    if entries_path.exists():
        entries = json.loads(entries_path.read_text(encoding="utf-8"))
        assert not any(entry["pid"] == pid for entry in entries), \
            f"запись о завершённом Word осталась в реестре: {entries}"


def _pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)
