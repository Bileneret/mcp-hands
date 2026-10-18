"""Диагностика стиля Quote: какие способы применения работают в этом Word.

Запуск: .venv\\Scripts\\python.exe -u spike\\diag_word_quote.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_hands.probe_worker import Worker  # noqa: E402


def attempt(label: str, fn) -> None:
    try:
        fn()
        print(f"  [ок ] {label}")
    except Exception as exc:  # noqa: BLE001 — предмет диагностики
        print(f"  [нет] {label}: {type(exc).__name__}: {str(exc)[:150]}")


def main() -> None:
    worker = Worker()
    worker.op_word_start()
    worker.op_word_new_document("Метод локальной автоматизации\nОсновной текст статьи.\n")
    doc = worker._word.ActiveDocument  # noqa: SLF001 — диагностика

    attempt("Styles(-185) существует и NameLocal",
            lambda: print("       NameLocal =", repr(doc.Styles(-185).NameLocal)))
    attempt("Styles(-185).InUse до применения",
            lambda: print("       InUse =", doc.Styles(-185).InUse))

    find = doc.Content.Find
    find.ClearFormatting()
    find.Execute("Метод локальной автоматизации", False, False, False, False, False, True, 1, False)
    rng = find.Parent

    attempt("rng.Paragraphs.Style = -185", lambda: setattr(rng.Paragraphs, "Style", -185))
    attempt("rng.Paragraphs.Style = 'Цитата'", lambda: setattr(rng.Paragraphs, "Style", "Цитата"))
    attempt("rng.Style = -185", lambda: setattr(rng, "Style", -185))
    attempt("rng.ParagraphFormat.OutlineLevel после", lambda: print("       OutlineLevel =",
            int(rng.Paragraphs(1).Range.ParagraphFormat.OutlineLevel)))
    attempt("ForEach-стиль: абзац(1).Style = doc.Styles(-185)",
            lambda: setattr(doc.Paragraphs(1), "Style", doc.Styles(-185)))
    attempt("Styles(-185).InUse после", lambda: print("       InUse =", doc.Styles(-185).InUse))
    attempt("имя абзаца(1).Style", lambda: print("       Style.NameLocal =",
            doc.Paragraphs(1).Range.Style.NameLocal))

    worker.op_word_quit()


if __name__ == "__main__":
    main()
