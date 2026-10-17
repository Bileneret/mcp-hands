"""Патч по итогам аудита v2 (`reports/audit_stage1_v2_20261017.md`).

Правки внесены после проверки на живом стенде: посылка про развёрнутые окна подтвердилась
не в том виде, как описана (DWM-границы у развёрнутого окна лежат в экране, а за экран
выходит `GetWindowRect`), но дыра реальна и закрывается так же.
"""

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


# ---------------- 1. screen.py: пересечение вместо строгого включения ----------------
patch("mcp_hands/screen.py", [
    ('''    @staticmethod
    def covers(frame: Frame, rect: dict) -> bool:
        """Полностью ли попадает область в кадр: проверка перед кропом."""
        offset_x, offset_y = frame.origin
        height, width = frame.image.shape[0], frame.image.shape[1]
        return (rect["left"] >= offset_x and rect["top"] >= offset_y
                and rect["right"] <= offset_x + width and rect["bottom"] <= offset_y + height)''',
     '''    @staticmethod
    def covers(frame: Frame, rect: dict) -> bool:
        """Видна ли область ЦЕЛИКОМ: строгая проверка, нужна для диагностики."""
        offset_x, offset_y = frame.origin
        height, width = frame.image.shape[0], frame.image.shape[1]
        return (rect["left"] >= offset_x and rect["top"] >= offset_y
                and rect["right"] <= offset_x + width and rect["bottom"] <= offset_y + height)

    @staticmethod
    def intersects(frame: Frame, rect: dict) -> bool:
        """Есть ли у области хоть какая-то видимая часть.

        Именно эта проверка решает, годится ли кадр для кропа: у развёрнутого окна
        рамка может выходить за пределы экрана (невидимые поля изменения размера),
        и требование полного включения запрещало бы снимать любое максимизированное окно.
        На экране видно только то, что поместилось, — остальное обрезает `crop`.
        """
        offset_x, offset_y = frame.origin
        height, width = frame.image.shape[0], frame.image.shape[1]
        inter_left = max(offset_x, rect["left"])
        inter_top = max(offset_y, rect["top"])
        inter_right = min(offset_x + width, rect["right"])
        inter_bottom = min(offset_y + height, rect["bottom"])
        return inter_right > inter_left and inter_bottom > inter_top'''),
    ('''        rect = space.window_frame(hwnd)
        base = frame or self._last
        if base is None or not self.covers(base, rect):
            base = self.grab(prefer="mss")
            if not self.covers(base, rect):
                raise ScreenCaptureError(
                    f"окно {rect} не попало даже в кадр всего виртуального экрана "
                    f"(начало {base.origin}, размер {base.size})")
        return self.crop(rect, base)''',
     '''        rect = space.window_frame(hwnd)
        base = frame or self._last
        if base is None or not self.intersects(base, rect):
            base = self.grab(prefer="mss")
            if not self.intersects(base, rect):
                raise ScreenCaptureError(
                    f"окно {rect} не попало даже в кадр всего виртуального экрана "
                    f"(начало {base.origin}, размер {base.size})")
        crop = self.crop(rect, base)
        if not self.covers(base, rect):
            crop.suspicious.append("окно видно не целиком: рамка выходит за пределы кадра")
        return crop'''),
], "пересечение вместо строгого включения: максимизированные окна")

# ---------------- 2. input.py: рабочий стол ввода, отчёт об ошибке SendInput ----------------
patch("mcp_hands/input.py", [
    ('''    def _send(self, *spec) -> int:''',
     '''    def attach_to_input_desktop(self) -> dict:
        """Привязывает поток к рабочему столу ввода.

        Если процесс запущен не в интерактивном рабочем столе (служба, задача планировщика,
        часть окружений IDE), `SendInput` завершается с `ERROR_ACCESS_DENIED`, а
        `GetCursorPos` возвращает (0,0). Переключение потока на рабочий стол ввода
        возвращает обоим функциям работоспособность.

        Вызывать до создания окон и хуков: `SetThreadDesktop` отказывается работать
        у потока, который уже что-то создал.
        """
        user32 = self.user32
        user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        user32.OpenInputDesktop.restype = wintypes.HANDLE
        user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]
        user32.SetThreadDesktop.restype = wintypes.BOOL
        user32.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                     ctypes.c_void_p, wintypes.DWORD,
                                                     ctypes.POINTER(wintypes.DWORD)]
        user32.CloseDesktop.argtypes = [wintypes.HANDLE]

        desktop = user32.OpenInputDesktop(0, False, 0x01FF)  # DESKTOP_ALL_ACCESS
        if not desktop:
            self.desktop = {"attached": False, "reason": f"OpenInputDesktop: {ctypes.get_last_error()}"}
            return self.desktop
        buffer = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD(0)
        user32.GetUserObjectInformationW(desktop, 2, buffer, ctypes.sizeof(buffer),  # UOI_NAME
                                         ctypes.byref(needed))
        attached = bool(user32.SetThreadDesktop(desktop))
        self.desktop = {"attached": attached, "name": buffer.value,
                        "reason": None if attached else f"SetThreadDesktop: {ctypes.get_last_error()}"}
        return self.desktop

    def _send(self, *spec) -> int:'''),
    ('''    def click(self, x: int, y: int, verify: bool = True, tolerance: int = 4,
              settle_s: float = 0.05) -> dict:
        """Перемещение, проверка позиции курсора, нажатие.

        Проверка курсора — страховка от промаха при смешении координатных пространств,
        но она не должна блокировать ввод там, где система вообще не отдаёт позицию:
        `GetCursorPos` возвращает (0,0) в неинтерактивной сессии. Тогда нажатие
        выполняется, а результат помечается `cursor_check: "недоступен"`.
        """
        self.move(x, y)
        if settle_s:
            time.sleep(settle_s)
        landed = self.cursor()
        cursor_available = not (landed == (0, 0) and (x, y) != (0, 0))
        if not cursor_available:
            self.move(x, y)  # повторная попытка: возможно, ввод ещё не обработан
            time.sleep(settle_s)
            landed = self.cursor()
            cursor_available = not (landed == (0, 0) and (x, y) != (0, 0))
        if verify and cursor_available and (
                abs(landed[0] - x) > tolerance or abs(landed[1] - y) > tolerance):
            return {"ok": False, "target": [x, y], "landed": list(landed),
                    "cursor_check": "промах",
                    "reason": "курсор не встал на цель — нажатие отменено"}
        sent = self._send((INPUT_MOUSE, MOUSEEVENTF_LEFTDOWN, 0, 0, 0),
                          (INPUT_MOUSE, MOUSEEVENTF_LEFTUP, 0, 0, 0))
        return {"ok": sent == 2, "target": [x, y], "landed": list(landed), "events": sent,
                "cursor_check": "проверен" if cursor_available else "недоступен"}''',
     '''    def click(self, x: int, y: int, verify: bool = True, tolerance: int = 4,
              settle_s: float = 0.05, allow_unverified: bool = False) -> dict:
        """Перемещение, проверка позиции курсора, нажатие.

        Нажатие отправляется С абсолютными координатами цели: даже если перемещение
        не отработало, система поставит курсор в нужную точку в момент щелчка.
        Иначе нажатие ушло бы в текущую позицию курсора, где может оказаться
        что угодно — например, кнопка закрытия чужого окна.

        Если позицию курсора подтвердить нельзя (неинтерактивный рабочий стол),
        нажатие блокируется: слепой клик в неизвестное место недопустим. Осознанный
        обход — только явным аргументом `allow_unverified=True`.
        """
        self.move(x, y)
        if settle_s:
            time.sleep(settle_s)
        landed = self.cursor()
        cursor_available = not (landed == (0, 0) and (x, y) != (0, 0))
        if not cursor_available:
            self.move(x, y)  # повторная попытка: возможно, ввод ещё не обработан
            time.sleep(settle_s)
            landed = self.cursor()
            cursor_available = not (landed == (0, 0) and (x, y) != (0, 0))
        if verify and not cursor_available and not allow_unverified:
            return {"ok": False, "target": [x, y], "landed": list(landed),
                    "cursor_check": "недоступен", "events": 0,
                    "reason": "позиция курсора не подтверждена — слепой клик заблокирован "
                              "(обход: allow_unverified=True)"}
        if verify and cursor_available and (
                abs(landed[0] - x) > tolerance or abs(landed[1] - y) > tolerance):
            return {"ok": False, "target": [x, y], "landed": list(landed),
                    "cursor_check": "промах", "events": 0,
                    "reason": "курсор не встал на цель — нажатие отменено"}
        normalized = self.space.to_normalized(x, y)
        flags = MOUSEEVENTF_LEFTDOWN | MOUSEEVENTF_ABSOLUTE
        sent = self._send((INPUT_MOUSE, flags, *normalized, 0),
                          (INPUT_MOUSE, flags | MOUSEEVENTF_LEFTUP, *normalized, 0))
        result = {"ok": sent == 2, "target": [x, y], "landed": list(landed), "events": sent,
                  "cursor_check": "проверен" if cursor_available else "недоступен"}
        if sent != 2:
            result["reason"] = (f"SendInput отправил {sent} из 2 событий: "
                                f"WinError {ctypes.get_last_error()} "
                                f"(5 — доступ запрещён: изоляция рабочего стола или UIPI)")
        return result'''),
], "привязка к рабочему столу ввода, абсолютные координаты нажатия, запрет слепого клика")

# ---------------- 3. probe.py: реестр процессов, проверка образа перед убийством ----------------
patch("mcp_hands/probe.py", [
    ('''    def track_process(self, pid: int, note: str = "") -> None:
        """Берёт процесс под опеку: он будет убит вместе с сателлитом.

        Нужно для COM-серверов Office: их запускает служба DCOM, ребёнком воркера они
        не являются, и при снятии сателлита остаются висеть, блокируя документы.
        """
        if pid and pid > 0:
            self._tracked[pid] = note''',
     '''    def track_process(self, pid: int, note: str = "", image: str = "") -> None:
        """Берёт процесс под опеку: он будет убит вместе с сателлитом.

        Нужно для COM-серверов Office: их запускает служба DCOM, ребёнком воркера они
        не являются, и при снятии сателлита остаются висеть, блокируя документы.

        `image` — ожидаемое имя исполняемого файла (например, `WINWORD.EXE`). Перед
        убийством образ проверяется: Windows переиспользует идентификаторы процессов,
        и убивать по одному лишь номеру опасно.
        """
        if pid and pid > 0:
            self._tracked[pid] = {"note": note, "image": image}'''),
    ('''    def untrack_process(self, pid: int) -> None:
        self._tracked.pop(pid, None)

    def tracked_processes(self) -> list[dict]:
        return [{"pid": pid, "note": note} for pid, note in self._tracked.items()]

    def kill_tracked(self) -> list[int]:
        """Принудительно завершает опекаемые процессы и возвращает список убитых."""
        import subprocess

        killed: list[int] = []
        for pid, _note in list(self._tracked.items()):
            try:
                # text=False: taskkill отвечает в кодировке консоли, и декодирование
                # как UTF-8 роняет поток чтения ещё до разбора результата.
                completed = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                           capture_output=True, timeout=10)
                if completed.returncode == 0:
                    killed.append(pid)
            except Exception:  # noqa: BLE001 — отсутствующий процесс это не ошибка
                pass
            self._tracked.pop(pid, None)
        return killed''',
     '''    def untrack_process(self, pid: int) -> None:
        self._tracked.pop(pid, None)

    def tracked_processes(self) -> list[dict]:
        return [{"pid": pid, **info} for pid, info in self._tracked.items()]

    @staticmethod
    def process_image_name(pid: int) -> str:
        """Имя исполняемого файла процесса: защита от убийства по переиспользованному PID."""
        import ctypes
        import ctypes.wintypes as wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                        wintypes.LPWSTR,
                                                        ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buffer))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return buffer.value.rsplit("\\\\", 1)[-1]
        finally:
            kernel32.CloseHandle(handle)
        return ""

    def kill_tracked(self) -> list[dict]:
        """Завершает опекаемые процессы; возвращает отчёт по каждому.

        Перед убийством проверяется имя образа: если оно не совпадает с записанным,
        процесс не наш — номер успел переиспользоваться, и трогать его нельзя.
        """
        import subprocess

        report: list[dict] = []
        for pid, info in list(self._tracked.items()):
            image = info.get("image", "")
            actual = self.process_image_name(pid)
            if not actual:
                report.append({"pid": pid, "killed": False, "reason": "процесса уже нет"})
            elif image and actual.lower() != image.lower():
                report.append({"pid": pid, "killed": False, "image": actual,
                               "reason": f"образ не совпал с ожидаемым {image}: PID переиспользован"})
            else:
                try:
                    # text=False: taskkill отвечает в кодировке консоли, и декодирование
                    # как UTF-8 роняет поток чтения ещё до разбора результата.
                    completed = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                               capture_output=True, timeout=10)
                    report.append({"pid": pid, "killed": completed.returncode == 0,
                                   "image": actual})
                except Exception as exc:  # noqa: BLE001
                    report.append({"pid": pid, "killed": False, "reason": str(exc)})
            self._tracked.pop(pid, None)
        return report

    # --- реестр процессов, созданных сателлитом ---------------------------

    def register_external(self, pid: int, image: str = "", note: str = "") -> None:
        """Регистрирует процесс, о котором сателлит сообщил через файл-реестр.

        Нужно потому, что COM-сервер (Word) создаётся ВНУТРИ воркера. Если воркер
        зависнет в блокирующем COM-вызове, он не успеет сообщить pid ответом на запрос,
        и Word утечёт. Поэтому воркер пишет pid в файл сразу после запуска, а супервизор
        читает файл в момент аварийного снятия — независимо от состояния воркера.
        """
        if pid and pid > 0 and pid not in self._tracked:
            self._tracked[pid] = {"note": note or "из реестра сателлита", "image": image}

    def sync_from_registry(self) -> list[dict]:
        """Подбирает процессы, записанные сателлитом в файл-реестр."""
        if not self.pid_file or not self.pid_file.exists():
            return []
        import json

        try:
            entries = json.loads(self.pid_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        added = []
        for entry in entries if isinstance(entries, list) else []:
            pid = int(entry.get("pid", 0))
            if pid and pid not in self._tracked:
                self.register_external(pid, entry.get("image", ""), entry.get("note", ""))
                added.append(entry)
        return added'''),
    ('''    def __init__(self, default_timeout_s: float = 1.0, name: str = "mcp-hands-probe") -> None:''',
     '''    def __init__(self, default_timeout_s: float = 1.0, name: str = "mcp-hands-probe",
                 pid_file: "Path | None" = None) -> None:
        import tempfile
        from pathlib import Path as _Path

        self.pid_file = _Path(pid_file) if pid_file else \\
            _Path(tempfile.gettempdir()) / f"{name}.pids.json"
        self.pid_file.unlink(missing_ok=True)'''),
    ('''        parent, child = multiprocessing.Pipe()
        self._connection = parent
        self._process = multiprocessing.Process(target=probe_worker.main, args=(child,),
                                                name=self.name, daemon=True)''',
     '''        parent, child = multiprocessing.Pipe()
        self._connection = parent
        self._process = multiprocessing.Process(target=probe_worker.main,
                                                args=(child, str(self.pid_file)),
                                                name=self.name, daemon=True)'''),
    ('''        tracked_killed = self.kill_tracked()
        if tracked_killed:
            self.stats.history.append({"op": "kill_tracked", "killed": tracked_killed})''',
     '''        self.sync_from_registry()  # подбираем то, о чём сателлит успел записать
        tracked_killed = self.kill_tracked()
        if tracked_killed:
            self.stats.history.append({"op": "kill_tracked", "processes": tracked_killed})'''),
    ('''    def stop(self) -> None:
        self.kill_tracked()''',
     '''    def stop(self) -> None:
        self.sync_from_registry()
        self.kill_tracked()
        self.pid_file.unlink(missing_ok=True)'''),
], "проверка образа перед убийством и файл-реестр процессов сателлита")

# ---------------- 4. probe_worker.py: рабочий стол ввода и реестр процессов ----------------
patch("mcp_hands/probe_worker.py", [
    ('''class Worker:
    """Состояние сателлита: клиент UIA, актуатор и координатное пространство создаются один раз."""

    def __init__(self) -> None:
        self.awareness = _enable_dpi()
        self._uia = None
        self._actuator = None
        self._space = None
        self.started_at = time.time()''',
     '''class Worker:
    """Состояние сателлита: клиент UIA, актуатор и координатное пространство создаются один раз."""

    def __init__(self, pid_file: str = "") -> None:
        self.awareness = _enable_dpi()
        self.pid_file = pid_file
        self._uia = None
        self._actuator = None
        self._space = None
        self.started_at = time.time()'''),
    ('''    def op_ping(self) -> dict:
        from . import __version__

        return {"version": __version__, "awareness": self.awareness,
                "pid": ctypes.windll.kernel32.GetCurrentProcessId()}''',
     '''    def op_ping(self) -> dict:
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
        if not hasattr(self, "_desktop_info"):
            from .input import Actuator

            probe = Actuator()
            self._desktop_info = probe.attach_to_input_desktop()
        return self._desktop_info

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
        entries = [entry for entry in entries if int(entry.get("pid", 0)) != pid]
        entries.append({"pid": pid, "image": image, "note": note})
        path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        return {"ok": True, "registered": pid, "total": len(entries)}'''),
    ('''def main(connection) -> None:
    """Цикл сателлита: читаем запрос, отвечаем, ждём следующий."""
    worker = Worker()''',
     '''def main(connection, pid_file: str = "") -> None:
    """Цикл сателлита: читаем запрос, отвечаем, ждём следующий."""
    worker = Worker(pid_file=pid_file)'''),
], "рабочий стол ввода при старте и файл-реестр процессов")
