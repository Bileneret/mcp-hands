"""Патч: операции Word в сателлите (шаг 4 этапа 1)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def patch(relative: str, replacements: list[tuple[str, str]], note: str) -> None:
    path = ROOT / relative
    text = path.read_text(encoding="utf-8")
    for old, new in replacements:
        if old not in text:
            raise SystemExit(f"не найден фрагмент в {relative}: {old[:70]!r}")
        text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")
    print(f"{relative}: {note}")


patch("mcp_hands/probe_worker.py", [
    ('''    def __init__(self, pid_file: str = "") -> None:
        self.awareness = _enable_dpi()''',
     '''    def __init__(self, pid_file: str = "") -> None:
        self.awareness = _enable_dpi()
        self._word = None
        self._word_version = ""'''),
    ('''    def op_unregister_process(self, pid: int) -> dict:''',
     '''    # --- Word через COM ---------------------------------------------------
    #
    # Все вызовы идут в этом процессе, поэтому его можно убить по бюджету. Word
    # поднимает служба DCOM: он не ребёнок воркера, и его pid регистрируется в файле
    # реестра сразу после запуска — до первой блокирующей операции.

    def _prepare_com(self) -> None:
        """Готовит поток к COM и глушит системные окна ошибок.

        `SetErrorMode` убирает диалоги «критическая ошибка приложения» и «отладка»:
        без него сбой внутри COM-сервера поднимает модальное окно, которое никто
        не закроет, и поток остаётся заблокированным навсегда.
        """
        import ctypes

        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002)  # FAILCRITICALERRORS | NOGPFAULTERRORBOX
        import pythoncom

        pythoncom.CoInitialize()

    @property
    def word(self):
        if self._word is None:
            raise RuntimeError("Word не запущен: сначала вызовите word_start")
        return self._word

    def op_word_start(self, visible: bool = False) -> dict:
        """Поднимает Word: без предупреждений, невидимым, и сообщает свой pid."""
        import win32com.client

        self._prepare_com()
        if self._word is not None:
            return {"pid": self._word_pid(), "reused": True}
        application = win32com.client.DispatchEx("Word.Application")
        application.Visible = bool(visible)
        # Предупреждения выключены до любой работы с документами: иначе «Сохранить
        # изменения?» или «Формат будет потерян» встаёт модальным окном.
        application.DisplayAlerts = 0
        self._word = application
        self._word_version = str(application.Version)
        pid = self._word_pid()
        registered = self.op_register_process(pid=pid, image="WINWORD.EXE",
                                              note=f"Word {self._word_version} через COM")
        return {"pid": pid, "version": self._word_version, "registry": registered}

    def _word_pid(self) -> int:
        """PID процесса Word: получаем из его же окна — так его видит и супервизор."""
        import ctypes
        import ctypes.wintypes as wintypes

        hwnd = int(self._word.Hwnd or 0)
        if not hwnd:
            return 0
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        return int(pid.value)

    def op_word_status(self) -> dict:
        if self._word is None:
            return {"running": False}
        documents = []
        try:
            count = int(self._word.Documents.Count)
            documents = [str(self._word.Documents(i + 1).Name) for i in range(count)]
        except Exception as exc:  # noqa: BLE001 — состояние может быть недоступно
            return {"running": True, "pid": self._word_pid(), "error": str(exc)}
        return {"running": True, "pid": self._word_pid(), "version": self._word_version,
                "documents": documents}

    def op_word_new_document(self, text: str = "") -> dict:
        document = self.word.Documents.Add()
        if text:
            document.Content.Text = text
        return {"name": str(document.Name), "characters": int(document.Content.Characters.Count)}

    def op_word_open(self, path: str) -> dict:
        """Открывает документ. Здесь чаще всего появляются модальные окна Word."""
        document = self.word.Documents.Open(path, ConfirmConversions=False, ReadOnly=False,
                                            AddToRecentFiles=False, Visible=False)
        return {"name": str(document.Name), "path": str(document.FullName),
                "paragraphs": int(document.Paragraphs.Count)}

    def op_word_replace(self, mapping: dict, match_case: bool = False) -> dict:
        """Заменяет плейсхолдеры во всём документе и возвращает число замен."""
        document = self.word.ActiveDocument
        replaced = {}
        for placeholder, value in mapping.items():
            find = document.Content.Find
            find.ClearFormatting()
            find.Replacement.ClearFormatting()
            find.Text = str(placeholder)
            find.Replacement.Text = str(value)
            find.Forward = True
            find.Wrap = 1  # wdFindContinue
            find.MatchCase = bool(match_case)
            outcome = find.Execute(Replace=2)  # wdReplaceAll
            replaced[str(placeholder)] = bool(outcome)
        return {"replaced": replaced,
                "characters": int(document.Content.Characters.Count)}

    def op_word_save_as(self, path: str, as_pdf: bool = False) -> dict:
        document = self.word.ActiveDocument
        if as_pdf:
            # 17 = wdFormatPDF. Экспорт печатает документ, поэтому Word занят и может
            # ответить «сервер занят» — вызывающий обязан повторить (см. office.py).
            document.ExportAsFixedFormat(OutputFileName=path, ExportFormat=17,
                                         OpenAfterExport=False, OptimizeFor=0)
        else:
            document.SaveAs2(path)
        import os

        return {"path": str(path), "exists": os.path.exists(path),
                "bytes": os.path.getsize(path) if os.path.exists(path) else 0}

    def op_word_quit(self, save: bool = False) -> dict:
        """Закрывает Word и снимает pid с учёта."""
        if self._word is None:
            return {"closed": True, "already": True}
        pid = self._word_pid()
        try:
            self._word.Quit(0 if not save else -1)  # 0 = wdDoNotSaveChanges
        finally:
            self._word = None
        if pid:
            self.op_unregister_process(pid=pid)
        return {"closed": True, "pid": pid}

    def op_unregister_process(self, pid: int) -> dict:'''),
], "операции Word через COM в сателлите")
