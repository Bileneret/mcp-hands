"""Патч по итогам аудита v4: рабочий стол мишени и диагностика чужого рабочего стола."""

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


# ---------------- 1. продукт: сравнение рабочих столов перед кликом ----------------
patch("mcp_hands/input.py", [
    ('''    @staticmethod
    def _desktop_name(handle) -> str:''',
     '''    def own_desktop(self) -> str:
        """Имя рабочего стола текущего потока."""
        desktop = self.user32.GetThreadDesktop(self.kernel32.GetCurrentThreadId())
        return self._desktop_name(desktop) if desktop else ""

    def window_desktop(self, hwnd: int) -> str:
        """Имя рабочего стола, на котором создано окно.

        Ввод уходит на рабочий стол вызывающего потока. Окно параллельного рабочего стола
        (служба, песочница, заблокированный сеанс) физически не получит ни мышь, ни
        клавиатуру — и это надо называть вслух, а не кликать в пустоту.
        """
        thread_id = self.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), None)
        desktop = self.user32.GetThreadDesktop(thread_id) if thread_id else 0
        return self._desktop_name(desktop) if desktop else ""

    @staticmethod
    def _desktop_name(handle) -> str:'''),
    ('''        target_hwnd = window_hwnd or node.get("hwnd") or 0
        if activate and target_hwnd:
            result["activation"] = self.activate(target_hwnd)''',
     '''        target_hwnd = window_hwnd or node.get("hwnd") or 0
        if target_hwnd:
            ours, theirs = self.own_desktop(), self.window_desktop(target_hwnd)
            result["desktop"] = {"ours": ours, "target": theirs}
            if ours and theirs and not ours.startswith("<") and not theirs.startswith("<") \\
                    and ours != theirs:
                return {**result, "ok": False,
                        "reason": f"окно цели на рабочем столе {theirs!r}, ввод идёт "
                                  f"на {ours!r}: клик туда не дойдёт"}
        if activate and target_hwnd:
            result["activation"] = self.activate(target_hwnd)'''),
], "отказ с объяснением, если окно цели на другом рабочем столе")

# ---------------- 2. мишень: создаётся на рабочем столе ввода ----------------
patch("mcp_hands/testing/fixture_window.py", [
    ('''def main() -> int:
    global _log_path
    with __import__("contextlib").suppress(OSError):
        ctypes.windll.shcore.SetProcessDpiAwareness(2)''',
     '''def attach_to_input_desktop() -> dict:
    """Создаёт окно там, куда придёт ввод.

    Если процесс запущен в песочнице или служебном сеансе, его окно оказывается на
    параллельном рабочем столе: щелчки, отправленные на рабочий стол ввода, до него
    не доходят, и тесты ввода ложно падают. Вызывать до создания окна.
    """
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]
    user32.SetThreadDesktop.restype = wintypes.BOOL
    user32.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                 ctypes.c_void_p, wintypes.DWORD,
                                                 ctypes.POINTER(wintypes.DWORD)]
    desktop = user32.OpenInputDesktop(0, False, 0x01FF)
    if not desktop:
        return {"attached": False, "reason": f"OpenInputDesktop: {ctypes.get_last_error()}"}
    attached = bool(user32.SetThreadDesktop(desktop))
    buffer = ctypes.create_unicode_buffer(256)
    needed = wintypes.DWORD(0)
    user32.GetUserObjectInformationW(desktop, 2, buffer, ctypes.sizeof(buffer),
                                     ctypes.byref(needed))  # UOI_NAME
    return {"attached": attached, "name": buffer.value,
            "reason": None if attached else f"SetThreadDesktop: {ctypes.get_last_error()}"}


def main() -> int:
    global _log_path
    with __import__("contextlib").suppress(OSError):
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    desktop = attach_to_input_desktop()'''),
    ('''    print(f"HWND={hwnd:#x}", flush=True)
    print(f"PID={os.getpid()}", flush=True)
    print(f"BUTTONS={','.join(name for name, *_ in BUTTONS)}", flush=True)''',
     '''    print(f"HWND={hwnd:#x}", flush=True)
    print(f"PID={os.getpid()}", flush=True)
    print(f"DESKTOP={desktop['name']}", flush=True)
    print(f"BUTTONS={','.join(name for name, *_ in BUTTONS)}", flush=True)'''),
], "мишень создаётся на рабочем столе ввода")

# ---------------- 3. conftest: фикстура сравнения столов ----------------
patch("tests/conftest.py", [
    ('''        self.hwnd = 0
        self.pid = 0
        self.ready = False''',
     '''        self.hwnd = 0
        self.pid = 0
        self.desktop = ""
        self.ready = False'''),
    ('''            elif line.startswith("PID="):
                self.pid = int(line.strip().split("=", 1)[1])''',
     '''            elif line.startswith("PID="):
                self.pid = int(line.strip().split("=", 1)[1])
            elif line.startswith("DESKTOP="):
                self.desktop = line.strip().split("=", 1)[1]'''),
    ('''@pytest.fixture
def target():''',
     '''def input_desktop_name() -> str:
    """Имя рабочего стола, на который уходит ввод в этом процессе."""
    from mcp_hands.geometry import CoordinateSpace
    from mcp_hands.input import Actuator

    return Actuator(CoordinateSpace()).own_desktop()


@pytest.fixture
def require_input_desktop(target):
    """Пропускает тест ввода, если окно мишени на другом рабочем столе.

    В песочнице или служебном сеансе окно мишени может оказаться на параллельном столе:
    тогда клики физически не доходят, и это отказ среды, а не поломка кода. Мишень
    старается создаться на столе ввода сама; если не удалось — тест честно пропускается
    с указанием обоих имён.
    """
    ours = input_desktop_name()
    if target.desktop and ours and target.desktop != ours:
        pytest.skip(f"мишень на рабочем столе {target.desktop!r}, ввод идёт на {ours!r}")


@pytest.fixture
def target():'''),
], "пропуск тестов ввода при разных рабочих столах")

# ---------------- 4. живые тесты ввода получают защиту ----------------
patch("tests/test_probe.py", [
    ('''def test_click_reaches_element(probe, target):''',
     '''def test_click_reaches_element(probe, target, require_input_desktop):'''),
], "защита от чужого рабочего стола")

patch("tests/test_regressions_audit_v2.py", [
    ('''def test_native_input_reaches_window(target):''',
     '''def test_native_input_reaches_window(target, require_input_desktop):'''),
], "защита от чужого рабочего стола")
