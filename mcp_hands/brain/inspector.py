"""L2-инспектор (этап 4, шаг 1): независимый арбитраж артефактов БЕЗ COM.

Контракт (аудит v18): L1 — подтверждение самим инструментом (перечитывание
свойств); L2 — «второе мнение», которое не доверяет COM-процессу: .docx и
.xlsx вскрываются как ZIP, интересное читается из XML (`word/document.xml`,
`xl/sharedStrings.xml`, `xl/worksheets/sheet1.xml`), PDF — через независимый
парсер (pypdf), плюс SHA-256 файла. Расхождение L1 и L2 — инцидент «disputed»,
а не молчаливое исправление.

Ограничение честности: это текстовый/структурный арбитраж, а не рендеринг —
форматирование (стили/шрифты) L2 не видит (это остаётся L1-проверками).
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path
from xml.etree import ElementTree

DOCX_TEXT_PART = "word/document.xml"
XLSX_SHARED_STRINGS = "xl/sharedStrings.xml"
XLSX_SHEET1 = "xl/worksheets/sheet1.xml"
XML_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


class InspectError(RuntimeError):
    """Инспектор не смог прочитать артефакт (битый файл, неизвестный формат)."""


def sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _zip_parts(path: Path) -> dict[str, str]:
    """Вскрывает Office-файл как ZIP: {имя части: текст}. Битый файл — InspectError."""
    parts: dict[str, str] = {}
    try:
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if name.endswith(".xml"):
                    parts[name] = archive.read(name).decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, OSError) as exc:
        raise InspectError(f"{path}: не читается как Office-упаковка: {exc}") from exc
    return parts


def _last_t(text: str, limit: int = 4000) -> str:
    """Конкатенация <w:t>…</w:t> документа (текст Word без разметки)."""
    chunks = []
    for token in text.split("<w:t"):
        if ">" not in token:
            continue
        body = token.split(">", 1)[1]
        if "</w:t>" in body:
            chunks.append(body.split("</w:t>", 1)[0])
    return "".join(chunks)[:limit]


def docx_text(path: str | Path) -> str:
    """Текст .docx из document.xml (без COM)."""
    parts = _zip_parts(Path(path))
    if DOCX_TEXT_PART not in parts:
        raise InspectError(f"{path}: не похоже на .docx (нет {DOCX_TEXT_PART})")
    return _last_t(parts[DOCX_TEXT_PART])


def xlsx_probe(path: str | Path) -> dict:
    """Проба .xlsx: значения листа 1 (строки/числа) и число диаграмм — без COM.

    Возвращает {"cells": {"A1": "..."}, "charts": N, "shared": N}.
    Строки хранятся в sharedStrings, числа — прямо в ячейках.
    """
    parts = _zip_parts(Path(path))
    shared_xml = parts.get(XLSX_SHARED_STRINGS, "")
    shared_strings: list[str] = []
    if shared_xml:
        root = ElementTree.fromstring(shared_xml)
        for item in root.findall(f"{XML_NS}si"):
            shared_strings.append("".join(node.text or "" for node in item.iter()
                                          if node.tag.endswith("}t")))
    if XLSX_SHEET1 not in parts:
        raise InspectError(f"{path}: не найден {XLSX_SHEET1}")
    sheet = ElementTree.fromstring(parts[XLSX_SHEET1])
    cells: dict[str, str] = {}
    for cell in sheet.iter(f"{XML_NS}c"):
        reference = cell.get("r", "")
        value_node = cell.find(f"{XML_NS}v")
        raw = value_node.text if value_node is not None else ""
        if cell.get("t") == "s" and raw != "":
            index = int(raw)
            cells[reference] = shared_strings[index] if index < len(shared_strings) else ""
        else:
            cells[reference] = raw or ""
    charts = sum(1 for name in parts if name.startswith("xl/charts/chart"))
    return {"cells": cells, "charts": charts, "shared": len(shared_strings)}


def inspect_artifact(path: str | Path) -> dict:
    """Автоматическая L2-проверка по суффиксу: текст/ячейки/хэш.

    Возвращает {"kind", "text_sample"/"cells", "sha256"}; для .pdf — извлечённый
    текст (pypdf). Ничего не вызывает из Office — инспектор независим.
    """
    target = Path(path)
    if not target.is_file():
        raise InspectError(f"артефакт не найден: {target}")
    suffix = target.suffix.lower()
    report: dict = {"path": str(target), "sha256": sha256(target)}
    if suffix == ".docx":
        report["kind"] = "docx"
        report["text_sample"] = docx_text(target)[:400]
    elif suffix == ".xlsx":
        report["kind"] = "xlsx"
        probe = xlsx_probe(target)
        report["cells"] = probe["cells"]
        report["charts"] = probe["charts"]
    elif suffix == ".pdf":
        report["kind"] = "pdf"
        from pypdf import PdfReader

        reader = PdfReader(str(target))
        report["text_sample"] = "\n".join(page.extract_text() or "" for page in reader.pages)[:400]
    else:
        report["kind"] = "file"
    return report
