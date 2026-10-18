"""Диагностика Excel-контура b09: запись → чтение в памяти → сохранение → чтение с диска.

Показывает, где теряются данные: на записи диапазона, на сохранении или на
переоткрытии. Запуск:
  .venv\\Scripts\\python.exe -u spike\\diag_excel.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_hands.probe_worker import Worker  # noqa: E402

OUT = Path(r"D:\BOT\MCP-Hands\logs\benchmark\stands\debug_excel.xlsx")


def main() -> None:
    worker = Worker()
    print("start:", worker.op_excel_start())
    print("new:", worker.op_excel_new_workbook("Свод"))
    sheet = worker._excel_book.Worksheets(1)  # noqa: SLF001 — диагностика

    # 0) состояние листа и приложения: не защищён ли, не в Protected View ли
    print("protect_contents:", sheet.ProtectContents, "| visible:", sheet.Visible)
    app = worker._excel  # noqa: SLF001
    try:
        print("protected_view_windows:", app.ProtectedViewWindows.Count)
        print("interactive:", getattr(app, "Interactive", "n/a"))
    except Exception as exc:  # noqa: BLE001
        print("состояние недоступно:", exc)

    # 1) скалярная запись
    sheet.Range("A1").Value2 = "скаляр!"
    print("скаляр A1:", repr(sheet.Range("A1").Value2))
    sheet.Cells(2, 1).Value = 42
    print("Cells(2,1):", repr(sheet.Cells(2, 1).Value2))
    print("quit:", worker.op_excel_quit())


if __name__ == "__main__":
    main()
