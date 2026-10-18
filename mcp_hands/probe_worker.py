"""Рабочая сторона процесса-сателлита: исполняет операции, которые могут зависнуть.

Запускается из `probe.ProbeProcess`, общается с родителем через канал `multiprocessing`.
Внутри — только тяжёлые и опасные вещи: UIA (чужой провайдер может подвиснуть),
COM-вызовы Office, физический ввод. Родитель убивает этот процесс по таймауту, поэтому
здесь нет ни сценариев, ни состояния, которое жалко потерять: перезапуск дешёвый
по сравнению с зависшим шагом агента.
"""

from __future__ import annotations

import ctypes
import time
import traceback


def _enable_dpi() -> str:
    with __import__("contextlib").suppress(OSError):
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    return "PER_MONITOR_DPI_AWARE"


class Worker:
    """Состояние сателлита: клиент UIA, актуатор и координатное пространство создаются один раз."""

    def __init__(self, pid_file: str = "") -> None:
        self.awareness = _enable_dpi()
        self._word = None
        self._word_version = ""
        self._word_process_id = 0
        # Строго до UIA и COM: comtypes создаёт скрытые окна для маршалинга, а поток
        # с окнами переключить рабочий стол уже нельзя (SetThreadDesktop вернёт 170).
        from .input import Actuator

        self._desktop_info = Actuator().attach_to_input_desktop()
        self.pid_file = pid_file
        self._uia = None
        self._actuator = None
        self._space = None
        self.started_at = time.time()

    # --- ленивая инициализация: не платим за то, что не понадобилось ---

    @property
    def uia(self):
        if self._uia is None:
            from .uia import UiaClient

            self._uia = UiaClient()
        return self._uia

    @property
    def actuator(self):
        if self._actuator is None:
            from .input import Actuator

            self._actuator = Actuator(self.space)
        return self._actuator

    @property
    def space(self):
        if self._space is None:
            from .geometry import CoordinateSpace

            self._space = CoordinateSpace()
        return self._space

    # --- операции ---------------------------------------------------------

    def handle(self, op: str, **kwargs) -> object:
        method = getattr(self, f"op_{op}", None)
        if method is None:
            raise ValueError(f"неизвестная операция: {op}")
        return method(**kwargs)

    def op_ping(self) -> dict:
        from . import __version__

        return {"version": __version__, "awareness": self.awareness,
                "desktop": self._desktop(),
                "pid": ctypes.windll.kernel32.GetCurrentProcessId()}

    def _desktop(self) -> dict:
        """Рабочий стол ввода: без него ввод в чужие окна не доходит.

        Процесс, запущенный вне интерактивного рабочего стола, получает от SendInput
        ERROR_ACCESS_DENIED, а GetCursorPos возвращает (0,0). Переключение потока
        на рабочий стол ввода выполняется до создания окон и COM-объектов.
        """
        return self._desktop_info

    # --- Word через COM ---------------------------------------------------
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

    @staticmethod
    def _winword_pids() -> set:
        """Список процессов WINWORD.EXE.

        `Application.Hwnd` при позднем связывании может быть недоступно, поэтому pid
        определяется сравнением списка процессов до и после запуска: `DispatchEx`
        создаёт НОВЫЙ экземпляр Word, и разница — это он.
        """
        import ctypes
        import ctypes.wintypes as wintypes

        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi.EnumProcesses.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
                                        ctypes.POINTER(wintypes.DWORD)]
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                        wintypes.LPWSTR,
                                                        ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        capacity = 2048
        buffer = (wintypes.DWORD * capacity)()
        needed = wintypes.DWORD(0)
        if not psapi.EnumProcesses(buffer, ctypes.sizeof(buffer), ctypes.byref(needed)):
            return set()
        found = set()
        for index in range(needed.value // ctypes.sizeof(wintypes.DWORD)):
            pid = int(buffer[index])
            if not pid:
                continue
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                continue
            try:
                name = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(len(name))
                if kernel32.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
                    if name.value.rsplit("\\", 1)[-1].upper() == "WINWORD.EXE":
                        found.add(pid)
            finally:
                kernel32.CloseHandle(handle)
        return found

    def op_word_start(self, visible: bool = False) -> dict:
        """Поднимает Word: без предупреждений, невидимым, и сообщает свой pid."""
        import win32com.client

        self._prepare_com()
        if self._word is not None:
            return {"pid": self._word_pid(), "reused": True}
        before = self._winword_pids()
        application = win32com.client.DispatchEx("Word.Application")
        application.Visible = bool(visible)
        # Предупреждения выключены до любой работы с документами: иначе «Сохранить
        # изменения?» или «Формат будет потерян» встаёт модальным окном.
        application.DisplayAlerts = 0
        self._word = application
        self._word_version = str(application.Version)
        new_pids = sorted(self._winword_pids() - before)
        self._word_process_id = new_pids[0] if len(new_pids) == 1 else self._word_pid()
        registered = self.op_register_process(pid=self._word_pid(), image="WINWORD.EXE",
                                              note=f"Word {self._word_version} через COM")
        return {"pid": self._word_pid(), "version": self._word_version,
                "registry": registered, "new_processes": new_pids}

    def _word_pid(self) -> int:
        """PID нашего экземпляра Word, определённый при запуске."""
        return int(getattr(self, "_word_process_id", 0) or 0)

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
        """Заменяет плейсхолдеры и подтверждает результат состоянием документа.

        Параметры `Find.Execute` передаются ПОЗИЦИОННО: при позднем связывании
        именованные аргументы приводят к тому, что вызов сообщает «найдено»,
        но замену не выполняет (проверено на живом Word). И главное: код возврата
        здесь ничего не доказывает — истина в том, остались ли плейсхолдеры в тексте.
        """
        document = self.word.ActiveDocument
        for placeholder, value in mapping.items():
            find = document.Content.Find
            find.ClearFormatting()
            find.Replacement.ClearFormatting()
            # FindText, MatchCase, MatchWholeWord, MatchWildcards, MatchSoundsLike,
            # MatchAllWordForms, Forward, Wrap (1 = wdFindContinue), Format,
            # ReplaceWith, Replace (2 = wdReplaceAll)
            find.Execute(str(placeholder), bool(match_case), False, False, False, False,
                         True, 1, False, str(value), 2)
        text = str(document.Content.Text)
        remaining = [str(placeholder) for placeholder in mapping if str(placeholder) in text]
        return {"remaining": remaining, "replaced_all": not remaining,
                "characters": int(document.Content.Characters.Count)}

    def op_word_format(self, actions: list) -> dict:
        """Форматирование документа: стили, шрифты, интервалы, оглавление.

        Каждая под-операция подтверждается ПЕРЕЧТЕНИЕМ свойства (а не кодом
        возврата): стиль найденного абзаца, имя/размер шрифта, правила интервалов,
        число таблиц оглавления. Ошибка одной под-операции не убивает остальные —
        она фиксируется в её записи (локализованный Word отказывает на чужих
        именах стилей: применяем builtin-коды, не зависящие от языка).
        """
        # wdBuiltinStyle: имена не зависят от локали Word.
        builtin_styles = {"Normal": -1, "Heading 1": -2, "Heading 2": -3, "Heading 3": -4,
                          "Heading 4": -5, "Heading 5": -6, "Heading 6": -7,
                          "Heading 7": -8, "Heading 8": -9, "Heading 9": -10,
                          "Title": -63, "Subtitle": -75, "Caption": -35}
        document = self.word.ActiveDocument
        applied: list[dict] = []

        def _find_range(text: str):
            find = document.Content.Find
            find.ClearFormatting()
            found = find.Execute(text, False, False, False, False, False, True, 1, False)
            return find.Parent if found else None

        for action in actions:
            op = str(action.get("op", ""))
            find_text = str(action.get("find", ""))
            record: dict = {"op": op, "find": find_text, "ok": False}
            try:
                if find_text:
                    rng = _find_range(find_text)
                    if rng is None:
                        record["reason"] = f"текст {find_text!r} не найден"
                        applied.append(record)
                        continue
                else:
                    rng = document.Content
                if op == "set_style":
                    name = str(action.get("style", "Normal"))
                    style_code = builtin_styles.get(name)
                    if style_code is not None:
                        # Стиль применяем к АБЗАЦАМ диапазона: назначение Range.Style
                        # в невидимом COM-документе не превращало абзац в заголовок
                        # (OutlineLevel оставался 10 — проверено диагностикой).
                        rng.Paragraphs.Style = style_code
                        outline = int(rng.Paragraphs(1).Range.ParagraphFormat.OutlineLevel)
                        is_heading = name.startswith("Heading")
                        heading_ok = (1 <= outline <= 9) if is_heading else (outline == 10)
                        record.update({"style": str(document.Styles(style_code).NameLocal),
                                       "outline_level": outline,
                                       "in_use": bool(document.Styles(style_code).InUse),
                                       "ok": bool(document.Styles(style_code).InUse) and heading_ok})
                        if not record["ok"] and is_heading:
                            record["reason"] = f"абзац не стал заголовком (уровень {outline})"
                    else:
                        rng.Paragraphs.Style = name  # локальное имя — на ответственности плана
                        record.update({"style": name, "ok": True})
                elif op == "set_font":
                    if action.get("name"):
                        rng.Font.Name = str(action["name"])
                    if action.get("size"):
                        rng.Font.Size = float(action["size"])
                    record.update({"font": str(rng.Font.Name), "size": float(rng.Font.Size),
                                   "ok": True})
                elif op == "set_spacing":
                    spacing = float(action.get("line_spacing", 1.0))
                    rule = {1.0: 0, 1.5: 1, 2.0: 2}.get(spacing, 5)  # 5 = wdLineSpaceMultiple
                    target = rng.ParagraphFormat
                    target.LineSpacingRule = rule
                    if rule == 5:
                        target.LineSpacing = 12.0 * spacing
                    if action.get("space_after") is not None:
                        target.SpaceAfter = float(action["space_after"])
                    record.update({"rule": int(target.LineSpacingRule),
                                   "after": float(target.SpaceAfter), "ok": True})
                elif op == "insert_toc":
                    anchor = rng
                    if find_text:
                        anchor.Collapse(0)  # wdCollapseEnd — оглавление ПОСЛЕ абзаца
                    # Add(Range, UseHeadingStyles, Upper, Lower, UseFields, TableID,
                    #     RightAlignPageNumbers, IncludePageNumbers)
                    toc = document.TablesOfContents.Add(anchor, True, 1, 3, False, "",
                                                        True, True)
                    toc.Update()
                    # ЛОВУШКА (проверено диагностикой): document.Repaginate() на
                    # невидимом COM-документе подвисает навсегда. Текст поля может
                    # остаться «Элементы оглавления не найдены», пока Word не
                    # переразобьёт страницы сам; честное подтверждение — сам факт
                    # существования таблицы оглавления (Count), а наполнение
                    # проверяет этап рендеринга/печати.
                    record.update({"toc_count": int(document.TablesOfContents.Count),
                                   "toc_text": str(toc.Range.Text)[:120], "ok": True})
                else:
                    record["reason"] = f"неизвестная под-операция {op!r}"
            except Exception as exc:  # noqa: BLE001 — фиксируем и идём дальше
                record["reason"] = f"{type(exc).__name__}: {str(exc)[:160]}"
            applied.append(record)

        toc_count = int(document.TablesOfContents.Count)
        return {"applied": applied, "all_ok": all(item.get("ok") for item in applied),
                "toc_count": toc_count, "paragraphs": int(document.Paragraphs.Count)}

    def op_word_close_document(self, save: bool = False) -> dict:
        """Закрывает активный документ, оставляя Word запущенным."""
        document = self.word.ActiveDocument
        name = str(document.Name)
        document.Close(0 if not save else -1)  # 0 = wdDoNotSaveChanges
        return {"closed": name, "documents_left": int(self.word.Documents.Count)}

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
        """Закрывает Word и снимает pid с учёта.

        Ссылка на COM-объект обнуляется ТОЛЬКО после успешного `Quit`. Иначе повтор
        (на занятость сервера Word отвечает ошибкой, и вызывающий повторяет) увидел бы
        `self._word is None` и отчитался фиктивным успехом — процесс остался бы висеть.
        """
        if self._word is None:
            return {"closed": True, "already": True}
        pid = self._word_pid()
        self._word.Quit(0 if not save else -1)  # 0 = wdDoNotSaveChanges
        self._word = None
        if pid:
            self.op_unregister_process(pid=pid)
        return {"closed": True, "pid": pid}

    def op_unregister_process(self, pid: int) -> dict:
        """Убирает процесс из файла-реестра при штатном завершении.

        Без этого после корректного `word.Quit()` в реестре остаётся запись, и при
        следующем аварийном снятии супервизор попытается убить уже несуществующий
        (или, хуже, переиспользованный) процесс.
        """
        import json

        if not self.pid_file:
            return {"ok": False, "reason": "файл-реестр не задан"}
        path = __import__("pathlib").Path(self.pid_file)
        if not path.exists():
            return {"ok": True, "removed": 0}
        with __import__("contextlib").suppress(ValueError, OSError):
            entries = json.loads(path.read_text(encoding="utf-8")) or []
        remaining = [entry for entry in entries if int(entry.get("pid", 0)) != pid]
        path.write_text(json.dumps(remaining, ensure_ascii=False), encoding="utf-8")
        return {"ok": True, "removed": len(entries) - len(remaining), "total": len(remaining)}

    def op_register_process(self, pid: int, image: str = "", note: str = "") -> dict:
        """Записывает процесс в файл-реестр: супервизор подберёт его при аварийном снятии.

        Вызывается сразу после запуска внешнего процесса (COM-сервера) и ДО блокирующего
        вызова: если воркер зависнет внутри COM, ответа родителю не будет, а запись
        в файле останется.
        """
        import json

        if not self.pid_file:
            return {"ok": False, "reason": "файл-реестр не задан"}
        path = __import__("pathlib").Path(self.pid_file)
        entries = []
        if path.exists():
            with __import__("contextlib").suppress(ValueError, OSError):
                entries = json.loads(path.read_text(encoding="utf-8")) or []
        from .probe import ProbeProcess

        identity = ProbeProcess.process_identity(pid)
        entries = [entry for entry in entries if int(entry.get("pid", 0)) != pid]
        entries.append({"pid": pid, "image": image or identity["image"],
                        "created": identity["created"], "note": note})
        path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        return {"ok": True, "registered": pid, "total": len(entries)}

    def op_windows(self, title_contains: str = "", exe: str = "") -> list[dict]:
        from . import uia

        if title_contains or exe:
            window = uia.find_window(title_contains=title_contains, exe=exe)
            return [window] if window else []
        return uia.enum_windows()

    def op_element(self, hwnd: int, name: str = "", control_type: str = "",
                   automation_id: str = "", scope: str = "shallow") -> dict | None:
        root, info = self.uia.content_root(hwnd, jump=True)
        node = self.uia.find(root, name=name, control_type=control_type,
                             automation_id=automation_id, scope=scope)
        if node is None:
            return None
        node["content_root"] = info
        return node

    def op_walk(self, hwnd: int, depth: int = 3, jump: bool = True, scope: str = "deep") -> dict:
        root, info = self.uia.content_root(hwnd, jump=jump)
        result = self.uia.walk(root, depth=depth, scope=scope)
        result["content_root"] = info
        if jump and info.get("how") != "window" and result["levels"] and \
                result["levels"][0]["elements"] == 0:
            # Document оказался листом (WinUI-текстовое поле) — обходим от окна.
            result = self.uia.walk(self.uia.window(hwnd), depth=depth, scope=scope)
            result["content_root"] = {**info, "fallback": "document без детей, обход от окна"}
        return result

    def op_click(self, hwnd: int, name: str = "", control_type: str = "",
                 automation_id: str = "", move_only: bool = False) -> dict:
        node = self.op_element(hwnd=hwnd, name=name, control_type=control_type,
                               automation_id=automation_id)
        if node is None:
            return {"ok": False, "reason": "элемент не найден"}
        if move_only:
            center = node["center"]
            self.actuator.move(*center)
            time.sleep(0.05)
            landed = self.actuator.cursor()
            return {"ok": True, "moved_only": True, "center": center, "landed": list(landed)}
        # PID берётся из самого узла UIA: обход всех окон системы ради того же числа
        # стоил десятки-сотни миллисекунд на каждый клик (замечание аудита v3).
        return self.actuator.click_node(node, pid=int(node.get("process_id") or 0),
                                        window_hwnd=hwnd)

    def op_type(self, text: str) -> dict:
        return self.actuator.type_text(text)

    def op_hotkey(self, keys: list[int]) -> dict:
        return self.actuator.hotkey(*keys)

    def op_activate(self, hwnd: int) -> dict:
        return self.actuator.activate(hwnd)

    def op_rights(self, pid: int) -> dict:
        return self.actuator.can_control(pid)

    def op_space(self) -> dict:
        return self.space.describe()

    def op_sleep(self, seconds: float = 1.0) -> dict:
        """Диагностика: заведомо долгая операция. Нужна тестам пути «бюджет истёк → убийство»."""
        time.sleep(float(seconds))
        return {"slept": seconds}

    def op_stats(self) -> dict:
        return {"uptime_s": round(time.time() - self.started_at, 1),
                "has_uia": self._uia is not None, "has_actuator": self._actuator is not None}


def main(connection, pid_file: str = "") -> None:
    """Цикл сателлита: читаем запрос, отвечаем, ждём следующий."""
    worker = Worker(pid_file=pid_file)
    try:
        while True:
            try:
                request = connection.recv()
            except (EOFError, OSError):
                return
            if request is None:
                return
            op = request.get("op", "")
            kwargs = request.get("kwargs", {})
            started = time.perf_counter()
            try:
                result = worker.handle(op, **kwargs)
                response = {"ok": True, "result": result,
                            "ms": round((time.perf_counter() - started) * 1000.0, 2)}
            except Exception as exc:  # noqa: BLE001 — ошибку отдаём родителю текстом
                response = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                            "traceback": traceback.format_exc()[-800:],
                            "ms": round((time.perf_counter() - started) * 1000.0, 2)}
            try:
                connection.send(response)
            except (BrokenPipeError, OSError):
                return
    finally:
        with __import__("contextlib").suppress(Exception):
            connection.close()


if __name__ == "__main__":  # прямой запуск для отладки: не используется ядром
    raise SystemExit("сателлит запускается только из mcp_hands.probe")
