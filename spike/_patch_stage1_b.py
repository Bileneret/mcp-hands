"""Патч №2 по итогам аудита этапа 1: захват, OCR, типы элементов, отслеживание процессов."""

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


# ---------------- screen.py ----------------
patch("mcp_hands/screen.py", [
    ('''@dataclass
class Frame:
    """Кадр в физических пикселях вместе с происхождением — иначе замеры не сравнить."""

    image: np.ndarray
    source: str
    ms: float
    rect: dict | None = None
    suspicious: list[str] = field(default_factory=list)''',
     '''class ScreenCaptureError(RuntimeError):
    """Ни один путь захвата не сработал: агенту нечем смотреть на экран."""


@dataclass
class Frame:
    """Кадр в физических пикселях вместе с происхождением — иначе замеры не сравнить.

    `origin` — координаты экрана, которым соответствует левый верхний пиксель кадра.
    У DXGI это первичный монитор, то есть (0, 0); у резервного пути через BitBlt —
    весь виртуальный рабочий стол, который может начинаться с отрицательных значений,
    если монитор стоит слева от основного. Без этого поля кроп окна на таком мониторе
    либо падает, либо возвращает чужую область.
    """

    image: np.ndarray
    source: str
    ms: float
    rect: dict | None = None
    origin: tuple[int, int] = (0, 0)
    suspicious: list[str] = field(default_factory=list)'''),
    ('''    def _grab_mss(self) -> np.ndarray:
        import mss

        with mss.mss() as session:
            shot = np.array(session.grab(session.monitors[1]))
        return shot[:, :, [2, 1, 0, 3]]  # BGRA''',
     '''    def _grab_mss(self) -> tuple[np.ndarray, tuple[int, int]]:
        """Резервный путь: BitBlt по всему виртуальному рабочему столу.

        Берётся `monitors[0]` (объединение мониторов), а не `monitors[1]` (первичный):
        кадр обязан покрывать окна на любом мониторе, а начало координат может быть
        отрицательным.
        """
        import mss

        with mss.mss() as session:
            area = session.monitors[0]
            shot = np.array(session.grab(area))
        return shot[:, :, [2, 1, 0, 3]], (area["left"], area["top"])  # BGRA'''),
    ('''    def grab(self, timeout_s: float = 0.25) -> Frame:''',
     '''    def grab(self, timeout_s: float = 0.25, prefer: str | None = None) -> Frame:'''),
    ('''        if image is None:
            image = self._grab_mss()
            source = f"mss (dxgi: {self._dxgi_error})"
        frame = Frame(image=image, source=source, ms=(time.perf_counter() - started) * 1000.0)''',
     '''        origin = (0, 0)
        if image is None or prefer == "mss":
            try:
                image, origin = self._grab_mss()
                source = "mss (весь виртуальный экран)" if prefer == "mss" \\
                    else f"mss (dxgi: {self._dxgi_error})"
            except Exception as exc:  # noqa: BLE001 — оба пути обязаны дать понятный отказ
                self.log.append({"op": "mss_failed", "error": f"{type(exc).__name__}: {exc}"})
                raise ScreenCaptureError(
                    "захват экрана недоступен: DXGI отдал ошибку "
                    f"({self._dxgi_error or 'не использовался'}), резервный BitBlt тоже "
                    f"({type(exc).__name__}: {exc})") from exc
        frame = Frame(image=image, source=source, origin=origin,
                      ms=(time.perf_counter() - started) * 1000.0)'''),
    ('''    def crop(self, rect: dict, frame: Frame | None = None) -> Frame:
        """Кроп области из имеющегося кадра: срез numpy, около 0.5 мс."""
        base = frame or self._last
        if base is None:
            raise RuntimeError("нет кадра: сначала вызовите grab()")
        left, top = max(0, rect["left"]), max(0, rect["top"])
        right = min(base.image.shape[1], rect["right"])
        bottom = min(base.image.shape[0], rect["bottom"])
        if right <= left or bottom <= top:
            raise ValueError(f"область кропа пуста: {rect}")
        started = time.perf_counter()
        patch = base.image[top:bottom, left:right].copy()
        return Frame(image=patch, source=f"{base.source}:crop",
                     ms=(time.perf_counter() - started) * 1000.0,
                     rect={"left": left, "top": top, "right": right, "bottom": bottom})''',
     '''    def crop(self, rect: dict, frame: Frame | None = None) -> Frame:
        """Кроп области экрана из имеющегося кадра: срез numpy, около 0.5 мс.

        Координаты области — экранные; они переводятся в индексы массива через
        начало захваченной области (`frame.origin`). На конфигурации с монитором слева
        от основного это единственный способ получить правильный кроп.
        """
        base = frame or self._last
        if base is None:
            raise RuntimeError("нет кадра: сначала вызовите grab()")
        offset_x, offset_y = base.origin
        height, width = base.image.shape[0], base.image.shape[1]
        left = max(0, min(width, rect["left"] - offset_x))
        top = max(0, min(height, rect["top"] - offset_y))
        right = max(0, min(width, rect["right"] - offset_x))
        bottom = max(0, min(height, rect["bottom"] - offset_y))
        if right <= left or bottom <= top:
            raise ScreenCaptureError(
                f"область {rect} вне захваченного кадра (начало {base.origin}, "
                f"размер {width}x{height}): нужен захват всего виртуального экрана "
                f"(grab(prefer='mss'))")
        started = time.perf_counter()
        patch = base.image[top:bottom, left:right].copy()
        return Frame(image=patch, source=f"{base.source}:crop", origin=(left + offset_x,
                                                                      top + offset_y),
                     ms=(time.perf_counter() - started) * 1000.0,
                     rect={"left": left + offset_x, "top": top + offset_y,
                           "right": right + offset_x, "bottom": bottom + offset_y})'''),
    ('''        issues: list[str] = []
        if image.max() <= _BLACK_THRESHOLD:
            issues.append("чёрный кадр")''',
     '''        issues: list[str] = []
        # Сравниваются только каналы цвета: альфа у захвата экрана всегда 255,
        # и по ней чёрный кадр неотличим от нормального.
        if image[:, :, :3].max() <= _BLACK_THRESHOLD:
            issues.append("чёрный кадр")'''),
], "отказ обоих путей, мультимониторный кроп, признак чёрного кадра")

# ---------------- ocr.py ----------------
patch("mcp_hands/ocr.py", [
    ('''        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        started = time.perf_counter()
        raw = asyncio.run(self._recognize(buffer.getvalue()))''',
     '''        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        started = time.perf_counter()
        raw = _run_sync(self._recognize(buffer.getvalue()))'''),
    ('''@dataclass
class Word:''',
     '''def _run_sync(coroutine):
    """Выполняет корутину из синхронного кода, даже если рядом уже крутится цикл событий.

    `asyncio.run` внутри работающего цикла падает с «event loop is already running»,
    поэтому в таком окружении распознавание уводится в отдельный поток.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    outcome: dict = {}

    def runner() -> None:
        try:
            outcome["value"] = asyncio.run(coroutine)
        except Exception as exc:  # noqa: BLE001 — ошибку вернём в вызывающий поток
            outcome["error"] = exc

    import threading

    thread = threading.Thread(target=runner, name="mcp-hands-ocr", daemon=True)
    thread.start()
    thread.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


@dataclass
class Word:'''),
], "распознавание из окружения с работающим циклом событий")

# ---------------- uia.py ----------------
patch("mcp_hands/uia.py", [
    ('''CONTROL_TYPE_IDS = {
    "button": UIA.UIA_ButtonControlTypeId,
    "edit": UIA.UIA_EditControlTypeId,
    "document": UIA.UIA_DocumentControlTypeId,
    "text": UIA.UIA_TextControlTypeId,
    "menu_item": UIA.UIA_MenuItemControlTypeId,
    "list_item": UIA.UIA_ListItemControlTypeId,
    "tab_item": UIA.UIA_TabItemControlTypeId,
}''',
     '''CONTROL_TYPE_IDS = {
    "button": UIA.UIA_ButtonControlTypeId,
    "calendar": UIA.UIA_CalendarControlTypeId,
    "check_box": UIA.UIA_CheckBoxControlTypeId,
    "combo_box": UIA.UIA_ComboBoxControlTypeId,
    "document": UIA.UIA_DocumentControlTypeId,
    "edit": UIA.UIA_EditControlTypeId,
    "group": UIA.UIA_GroupControlTypeId,
    "hyperlink": UIA.UIA_HyperlinkControlTypeId,
    "image": UIA.UIA_ImageControlTypeId,
    "list": UIA.UIA_ListControlTypeId,
    "list_item": UIA.UIA_ListItemControlTypeId,
    "menu": UIA.UIA_MenuControlTypeId,
    "menu_bar": UIA.UIA_MenuBarControlTypeId,
    "menu_item": UIA.UIA_MenuItemControlTypeId,
    "pane": UIA.UIA_PaneControlTypeId,
    "progress_bar": UIA.UIA_ProgressBarControlTypeId,
    "radio_button": UIA.UIA_RadioButtonControlTypeId,
    "scroll_bar": UIA.UIA_ScrollBarControlTypeId,
    "separator": UIA.UIA_SeparatorControlTypeId,
    "slider": UIA.UIA_SliderControlTypeId,
    "spinner": UIA.UIA_SpinnerControlTypeId,
    "split_button": UIA.UIA_SplitButtonControlTypeId,
    "status_bar": UIA.UIA_StatusBarControlTypeId,
    "tab": UIA.UIA_TabControlTypeId,
    "tab_item": UIA.UIA_TabItemControlTypeId,
    "text": UIA.UIA_TextControlTypeId,
    "thumb": UIA.UIA_ThumbControlTypeId,
    "title_bar": UIA.UIA_TitleBarControlTypeId,
    "tool_bar": UIA.UIA_ToolBarControlTypeId,
    "tool_tip": UIA.UIA_ToolTipControlTypeId,
    "tree": UIA.UIA_TreeControlTypeId,
    "tree_item": UIA.UIA_TreeItemControlTypeId,
    "window": UIA.UIA_WindowControlTypeId,
}'''),
], "расширенный список типов элементов")

# ---------------- probe.py ----------------
patch("mcp_hands/probe.py", [
    ('''    def __init__(self, default_timeout_s: float = 1.0, name: str = "mcp-hands-probe") -> None:
        self.default_timeout_s = default_timeout_s
        self.name = name
        self.stats = ProbeStats()
        self._process: multiprocessing.Process | None = None
        self._connection = None''',
     '''    def __init__(self, default_timeout_s: float = 1.0, name: str = "mcp-hands-probe") -> None:
        self.default_timeout_s = default_timeout_s
        self.name = name
        self.stats = ProbeStats()
        self._process: multiprocessing.Process | None = None
        self._connection = None
        # Процессы, запущенные не как дети воркера: COM-серверы Office поднимает служба
        # DCOM, поэтому убийство воркера их не задевает. Их надо отслеживать отдельно,
        # иначе после каждого снятия остаётся висящий WINWORD.EXE с занятым файлом.
        self._tracked: dict[int, str] = {}'''),
    ('''    def kill(self) -> None:
        """Аварийное снятие: единственный способ разблокировать зависший вызов UIA."""''',
     '''    # --- отслеживаемые процессы ------------------------------------------

    def track_process(self, pid: int, note: str = "") -> None:
        """Берёт процесс под опеку: он будет убит вместе с сателлитом.

        Нужно для COM-серверов Office: их запускает служба DCOM, ребёнком воркера они
        не являются, и при снятии сателлита остаются висеть, блокируя документы.
        """
        if pid and pid > 0:
            self._tracked[pid] = note

    def untrack_process(self, pid: int) -> None:
        self._tracked.pop(pid, None)

    def tracked_processes(self) -> list[dict]:
        return [{"pid": pid, "note": note} for pid, note in self._tracked.items()]

    def kill_tracked(self) -> list[int]:
        """Принудительно завершает опекаемые процессы и возвращает список убитых."""
        import subprocess

        killed: list[int] = []
        for pid, _note in list(self._tracked.items()):
            try:
                completed = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                           capture_output=True, text=True, timeout=10)
                if completed.returncode == 0:
                    killed.append(pid)
            except Exception:  # noqa: BLE001 — отсутствующий процесс это не ошибка
                pass
            self._tracked.pop(pid, None)
        return killed

    def kill(self) -> None:
        """Аварийное снятие: единственный способ разблокировать зависший вызов UIA."""'''),
    ('''        if self._process is not None:
            self._process.kill()
            self._process.join(1.0)
            self._process = None
        if self._connection is not None:
            try:
                self._connection.close()
            except OSError:
                pass
            self._connection = None
        self.stats.restarts += 1''',
     '''        tracked_killed = self.kill_tracked()
        if tracked_killed:
            self.stats.history.append({"op": "kill_tracked", "killed": tracked_killed})
        if self._process is not None:
            self._process.kill()
            self._process.join(1.0)
            self._process = None
        if self._connection is not None:
            try:
                self._connection.close()
            except OSError:
                pass
            self._connection = None
        self.stats.restarts += 1'''),
    ('''    def stop(self) -> None:
        if self._connection is not None:''',
     '''    def stop(self) -> None:
        self.kill_tracked()
        if self._connection is not None:'''),
], "опека COM-процессов: убийство вместе с сателлитом")
