"""Диагностика чтения свойств Word при позднем связывании (отладка b02).

Русский Word отказывал на строковом "Heading 1" (локализованные стили);
после перехода на builtin-коды стиль применяется, но ЧТЕНИЕ `rng.Style.Name`,
`rng.Font.Name`, `ParagraphFormat` под вопросом — свойства возвращаются как
`<unknown>`. Скрипт гоняет Worker напрямую (диагностический доступ к `_word`),
пробует каждый способ чтения и печатает вердикт: что можно использовать в
подтверждениях op_word_format, а что — только фиксировать фактом применения.

Запуск: .venv\\Scripts\\python.exe -u spike\\diag_word_style.py <черновик.docx>
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_hands.probe_worker import Worker  # noqa: E402


def try_read(label: str, getter) -> None:
    try:
        value = getter()
        print(f"  [ок ] {label}: {value!r}")
    except Exception as exc:  # noqa: BLE001 — предмет диагностики
        print(f"  [нет] {label}: {type(exc).__name__}: {str(exc)[:120]}")


def main(path: str) -> None:
    worker = Worker()
    started = worker.op_word_start()
    print("word:", started)
    opened = worker.op_word_open(path)
    print("open:", opened)
    document = worker._word.ActiveDocument  # noqa: SLF001 — диагностика

    find = document.Content.Find
    find.ClearFormatting()
    found = find.Execute("Введение", False, False, False, False, False, True, 1, False)
    rng = find.Parent if found else document.Content
    print("нашли 'Введение':", bool(found))
    rng.Paragraphs.Style = -2  # wdStyleHeading1 — применяем ко ВСЕМ абзацам диапазона
    print("стиль применён к абзацам без ошибок COM")
    try_read("OutlineLevel абзаца", lambda: int(rng.Paragraphs(1).Range.ParagraphFormat.OutlineLevel))
    try_read("Styles(-2).InUse", lambda: document.Styles(-2).InUse)

    try_read("rng.Style", lambda: rng.Style)
    try_read("rng.Style.Name", lambda: rng.Style.Name)
    try_read("rng.ParagraphStyle.Name", lambda: rng.ParagraphStyle.Name)
    try_read("document.Styles(-2).Name", lambda: document.Styles(-2).Name)
    try_read("document.Styles(-2).NameLocal", lambda: document.Styles(-2).NameLocal)
    try_read("rng.Font.Name", lambda: rng.Font.Name)
    try_read("rng.Font.Size", lambda: rng.Font.Size)
    rng.ParagraphFormat.LineSpacingRule = 1  # wdLineSpace1pt5
    try_read("ParagraphFormat.LineSpacingRule", lambda: rng.ParagraphFormat.LineSpacingRule)

    anchor = document.Content
    toc = document.TablesOfContents.Add(anchor, True, 1, 3, False, "", True, True)
    toc.Update()
    # Repaginate НЕ вызывать: на невидимом COM-документе подвисает навсегда
    # (поймано живым прогоном 18.10) — тексту поля даём осесть самому.
    try_read("TOC.Range.Text[:300]", lambda: toc.Range.Text[:300])
    try_read("TOC.Count", lambda: document.TablesOfContents.Count)
    try_read("ParagraphFormat.OutlineLevel абзаца 'Введение'",
             lambda: rng.ParagraphFormat.OutlineLevel)
    try_read("Styles(-2).InUse", lambda: document.Styles(-2).InUse)
    try_read("Styles(-2).NameLocal", lambda: document.Styles(-2).NameLocal)

    print("quit:", worker.op_word_quit())


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1
         else r"D:\BOT\MCP-Hands\logs\benchmark\stands\debug\черновик.docx")
