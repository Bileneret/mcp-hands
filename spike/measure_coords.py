"""CoordinateSpace: три пространства координат, ловушка смешения и живой клик.

Пространства:
  * UIA `BoundingRectangle` и `GetWindowRect` — глазами процесса (при DPI-awareness ≠ V2
    Windows отдаёт их виртуализованными: логические единицы 96 dpi);
  * физические пиксели DXGI — кадр всегда в физических пикселях, виртуализация на него
    не действует;
  * нормализованные 0..65535 для `SendInput` (`MOUSEEVENTF_ABSOLUTE`).

Ключевое: сама виртуализация согласована — UIA-координаты и метрики экрана ужимаются
одинаково, поэтому клик по UIA-координатам попадает даже из unaware-процесса. Ловушка
возникает при **смешении пространств**, а оно неизбежно у пиксельного пути: VLM находит
элемент на кадре DXGI в физических пикселях, а нормализует их процесс, который видит
виртуализованные метрики. Промах равен коэффициенту масштаба (25% при 125%) — в разы
больше типичной кнопки.

Проверяется фактом: скрипт кликает по координатам, а тестовое окно (`fixture_window.py`)
пишет, какая кнопка получила нажатие и в какой физической точке оказался курсор.

    python spike/measure_coords.py            # оба варианта + живые клики
    python spike/measure_coords.py --no-click # только измерения
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wintypes
import json
import subprocess
import sys
import time
from pathlib import Path

SPIKE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SPIKE_DIR))

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
BUTTONS = ("alpha", "beta", "gamma")


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class INPUT(ctypes.Structure):
    class _UNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _UNION)]


KEYEVENTF_KEYUP = 0x0002
VK_MENU = 0x12
INPUT_KEYBOARD = 1


def prototypes() -> tuple:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = ctypes.c_uint
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = ctypes.c_bool
    # Без restype ctypes усекает 64-битный HWND до int, и сравнение с ним всегда ложно.
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.AttachThreadInput.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_bool]
    return user32, ctypes.WinDLL("shcore", use_last_error=True)


# --- измерения --------------------------------------------------------------


def metrics(user32) -> dict:
    """Метрики экрана, как их видит ТЕКУЩИЙ процесс (у unaware-процесса — виртуализованные)."""
    get = user32.GetSystemMetrics
    return {"screen": [get(0), get(1)],
            "virtual": [get(76), get(77), get(78), get(79)]}


def process_dpi(user32) -> dict:
    value = ctypes.c_int(-1)
    shcore = ctypes.WinDLL("shcore", use_last_error=True)
    shcore.GetProcessDpiAwareness.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
    shcore.GetProcessDpiAwareness.restype = ctypes.c_long
    shcore.GetProcessDpiAwareness(None, ctypes.byref(value))
    hwnd = user32.GetForegroundWindow()
    return {"awareness_code": value.value,
            "foreground_dpi": user32.GetDpiForWindow(hwnd) if hwnd else 96}


def element_rect(uia, hwnd: int, name: str) -> dict | None:
    from comtypes.gen import UIAutomationClient as UIA

    condition = uia.CreatePropertyCondition(UIA.UIA_NamePropertyId, name)
    element = uia.ElementFromHandle(hwnd).FindFirst(UIA.TreeScope_Descendants, condition)
    if not element:
        return None
    rect = element.CurrentBoundingRectangle
    return {"left": rect.left, "top": rect.top, "right": rect.right, "bottom": rect.bottom,
            "center": [(rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2]}


def normalize(x: int, y: int, screen: dict) -> tuple[int, int]:
    """Физические пиксели → 0..65535 по метрикам, которые видит САМ процесс."""
    left, top, width, height = screen["virtual"]
    return (round((x - left) * 65535 / max(1, width - 1)),
            round((y - top) * 65535 / max(1, height - 1)))


# --- ввод -------------------------------------------------------------------


def send_events(user32, *spec) -> int:
    events = (INPUT * len(spec))()
    for index, (flags, x, y) in enumerate(spec):
        events[index].type = INPUT_MOUSE
        events[index].mi = MOUSEINPUT(x, y, 0, flags, 0, None)
    return user32.SendInput(len(spec), events, ctypes.sizeof(INPUT))


def tap_alt(user32) -> None:
    """Нажатие Alt снимает блокировку передачи фокуса, из-за которой SetForegroundWindow молчит."""
    for flags in (0, KEYEVENTF_KEYUP):
        event = (INPUT * 1)()
        event[0].type = INPUT_KEYBOARD
        event[0].ki = KEYBDINPUT(VK_MENU, 0, flags, 0, None)
        user32.SendInput(1, event, ctypes.sizeof(INPUT))


def ensure_foreground(user32, kernel32, hwnd: int, timeout: float = 1.5) -> bool:
    """Делает окно активным: иначе первый клик съедается активацией и до кнопки не доходит.

    SetForegroundWindow срабатывает не всегда (Windows отдаёт фокус только «своему»
    процессу), поэтому пробуем три приёма: прямой вызов, присоединение к потоку активного
    окна и нажатие Alt.
    """
    handle = wintypes.HWND(hwnd)
    if _foreground_hwnd(user32) == hwnd:
        return True
    user32.ShowWindow(handle, 9)  # SW_RESTORE
    kernel32.GetCurrentThreadId.restype = ctypes.c_ulong
    kernel32.GetCurrentThreadId.argtypes = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        current_thread = kernel32.GetCurrentThreadId()
        foreground_thread = user32.GetWindowThreadProcessId(wintypes.HWND(_foreground_hwnd(user32)), None)
        attached = bool(foreground_thread) and foreground_thread != current_thread
        if attached:
            user32.AttachThreadInput(current_thread, foreground_thread, True)
        try:
            user32.SetForegroundWindow(handle)
            if _foreground_hwnd(user32) == hwnd:
                return True
        finally:
            if attached:
                user32.AttachThreadInput(current_thread, foreground_thread, False)
        tap_alt(user32)
        time.sleep(0.15)
    return _foreground_hwnd(user32) == hwnd


def _foreground_hwnd(user32) -> int:
    """HWND активного окна как целое: c_void_p-объекты между собой не сравниваются."""
    return int(user32.GetForegroundWindow() or 0)


def cursor_pos(user32) -> tuple[int, int]:
    point = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


def click_verified(user32, norm: tuple[int, int], tolerance: int = 4) -> dict:
    """Двухфазный клик: сначала перемещение и ПРОВЕРКА позиции курсора, потом нажатие.

    Пакет из трёх событий на неактивном окне смещает курсор, поэтому перемещение
    и нажатие разделены: позиция подтверждается до того, как что-то нажато.
    """
    send_events(user32, (MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, *norm))
    time.sleep(0.05)
    landed = cursor_pos(user32)
    screen = metrics(user32)["virtual"]
    expected = (round(norm[0] * (screen[2] - 1) / 65535), round(norm[1] * (screen[3] - 1) / 65535))
    if abs(landed[0] - expected[0]) > tolerance or abs(landed[1] - expected[1]) > tolerance:
        return {"moved": False, "landed": list(landed), "cursor_expected": list(expected),
                "note": "курсор не встал на вычисленную точку — нажатие отменено"}
    send_events(user32, (MOUSEEVENTF_LEFTDOWN, 0, 0), (MOUSEEVENTF_LEFTUP, 0, 0))
    return {"moved": True, "landed": list(landed), "cursor_expected": list(expected)}


# --- ребёнок: измерения и клики одного варианта -----------------------------


def run_variant(hwnd: int, targets: dict, click: bool, click_log: Path, aware: bool) -> dict:
    import uia_common

    dpi_call = "не вызывался"
    if aware:
        import dpi_aware

        dpi_call = dpi_aware.enable()

    user32, _ = prototypes()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(ctypes.c_ulong)]
    user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
    saved = cursor_pos(user32)
    uia = uia_common.create_uia()

    report = {
        "variant": "v2" if aware else "unaware",
        "dpi_call": dpi_call,
        "process_dpi": process_dpi(user32),
        "metrics": metrics(user32),
        "buttons_uia": {name: element_rect(uia, hwnd, name) for name in BUTTONS},
    }

    # Активация окна: первый клик по неактивному окну съедается активацией,
    # до кнопки нажатие не доходит (проверено — промах без записи о нажатии).
    report["activated"] = ensure_foreground(user32, kernel32, hwnd)
    time.sleep(0.2)

    if click:
        report["clicks"] = []
        for name, physical in targets.items():
            norm = normalize(*physical, report["metrics"])
            outcome = click_verified(user32, norm)
            time.sleep(0.25)
            logged = click_log.read_text(encoding="utf-8").strip().splitlines()
            last = logged[-1] if logged else "<промах: окно ничего не получило>"
            report["clicks"].append({
                "target_name": name,
                "physical_target": list(physical),
                "normalized": list(norm),
                **outcome,
                "log": last,
                "ok": last.startswith(f"CLICK {name} "),
            })
            click_log.write_text("", encoding="utf-8")
        user32.SetCursorPos(saved[0], saved[1])

    return report


# --- родитель ---------------------------------------------------------------


def start_fixture(click_log: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-u", str(SPIKE_DIR / "fixture_window.py"), "--click-log", str(click_log)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(description="CoordinateSpace: замеры и живой клик")
    parser.add_argument("--variant", choices=("v2", "unaware"))
    parser.add_argument("--hwnd")
    parser.add_argument("--targets", help="name=x,y;name=x,y — физические цели кликов")
    parser.add_argument("--click-log", default=str(SPIKE_DIR / ".clicks.log"))
    parser.add_argument("--no-click", action="store_true")
    parser.add_argument("--json", default=str(SPIKE_DIR / "results_coords.json"))
    args = parser.parse_args()

    click_log = Path(args.click_log)
    click_log.write_text("", encoding="utf-8")

    if args.variant and args.hwnd:
        targets = {}
        for chunk in (args.targets or "").split(";"):
            if "=" in chunk:
                name, point = chunk.split("=", 1)
                x, y = point.split(",")
                targets[name] = (int(x), int(y))
        report = run_variant(int(args.hwnd, 16), targets, not args.no_click, click_log,
                             aware=args.variant == "v2")
        print("@@RESULT@@" + json.dumps(report, ensure_ascii=False), flush=True)
        return 0

    fixture = start_fixture(click_log)
    results: dict = {}
    try:
        hwnd = None
        deadline = time.time() + 15
        while time.time() < deadline:
            line = fixture.stdout.readline()
            if line.startswith("HWND="):
                hwnd = line.strip().split("=", 1)[1]
                break
            if not line:
                break
        if not hwnd:
            print("фикстура не сообщила HWND")
            return 1
        time.sleep(0.6)

        import dpi_aware
        import uia_common

        dpi_aware.enable()
        user32, _ = prototypes()
        uia = uia_common.create_uia()
        rects = {name: element_rect(uia, int(hwnd, 16), name) for name in BUTTONS}
        missing = [name for name, rect in rects.items() if rect is None]
        if missing:
            print(f"кнопки не найдены в UIA: {missing}")
            return 1
        # Цели — в физических пикселях, как их видит пиксельный путь (кадр DXGI).
        targets = {name: tuple(rect["center"]) for name, rect in rects.items()}
        window_rect = uia_common.window_rect(int(hwnd, 16))

        for variant in ("unaware", "v2"):
            command = [sys.executable, "-u", str(Path(__file__)), "--variant", variant,
                       "--hwnd", hwnd, "--click-log", str(click_log),
                       "--targets", ";".join(f"{name}={x},{y}" for name, (x, y) in targets.items())]
            if args.no_click:
                command.append("--no-click")
            outcome = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                     errors="replace", creationflags=CREATE_NO_WINDOW)
            for line in outcome.stdout.splitlines():
                if line.startswith("@@RESULT@@"):
                    results[variant] = json.loads(line[len("@@RESULT@@"):])
            if variant not in results:
                print(f"{variant}: замер не удался\n{outcome.stderr[-500:]}")
    finally:
        fixture.kill()
        fixture.communicate()
        click_log.unlink(missing_ok=True)

    report = {
        "physical_targets": targets,
        "buttons_uia_aware": rects,
        "window_win32_aware": window_rect,
        "variants": results,
    }
    Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"Цели (физические пиксели, как в кадре DXGI): {targets}")
    print(f"Окно (Win32 из aware-процесса): {window_rect}")
    for variant, data in results.items():
        print(f"\n=== вариант {variant} ({data['dpi_call']}, "
              f"awareness={data['process_dpi']['awareness_code']}) ===")
        print(f"  метрики процесса: экран {data['metrics']['screen']}, "
              f"виртуальный {data['metrics']['virtual']}")
        for name, rect in data["buttons_uia"].items():
            if rect:
                print(f"  кнопка {name:<6} UIA в этом процессе: центр {rect['center']}")
        print(f"  окно активировано: {data.get('activated')}")
        for click in data.get("clicks", []):
            mark = "ПОПАЛ" if click["ok"] else "ПРОМАХ"
            print(f"  {click['target_name']:<6} цель {click['physical_target']} "
                  f"-> норм {click['normalized']} -> курсор процесса {click.get('landed')} "
                  f"-> {click['log'][:44]} [{mark}]")
    print(f"\nРезультат: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
