"""Прототип замкнутого цикла: «увидел экран → нашёл элемент → кликнул → проверил».

Критерий выхода этапа 0 — рабочий прототип, а не набор замеров. Здесь собираются вместе
все слои спайка:

  * ВИДЕЛ  — кадр монитора через DXGI (`capture_dxgi`) и прямоугольник окна;
  * НАШЁЛ  — адресный поиск элемента в UIA за один межпроцессный переход (кэш-реквест,
              `ConnectionTimeout` 500 мс, при зависшем провайдере — пиксельный фоллбэк);
  * КЛИКНУЛ — активация окна и клик в два шага (перемещение → проверка курсора → нажатие);
  * ПРОВЕРИЛ — постусловие: изменилось ли состояние элемента после действия.

Задачи берутся из аргументов, чтобы прототип можно было наводить на разные цели:

    python spike/prototype_loop.py --task "alpha" --expect-click
    python spike/prototype_loop.py --window "MCP-Hands" --task alpha --json out.json
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wintypes
import json
import sys
import time
from pathlib import Path

SPIKE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SPIKE_DIR))

import dpi_aware  # noqa: E402
import measure_coords as mc  # noqa: E402
import uia_common  # noqa: E402
from comtypes.gen import UIAutomationClient as UIA  # noqa: E402

CONNECTION_TIMEOUT_MS = 500


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Прототип: увидел → нашёл → кликнул → проверил")
    parser.add_argument("--window", default="MCP-Hands", help="подстрока заголовка окна")
    parser.add_argument("--task", required=True, help="имя элемента, по которому надо кликнуть")
    parser.add_argument("--control-type", choices=("button", "text", "any"), default="button")
    parser.add_argument("--expect-click", action="store_true",
                        help="постусловие: проверить, что нажатие зарегистрировано (журнал фикстуры)")
    parser.add_argument("--click-log", default=str(SPIKE_DIR / ".clicks.log"))
    parser.add_argument("--dry-run", action="store_true", help="найти и показать, но не кликать")
    parser.add_argument("--repeat", type=int, default=1,
                        help="сколько циклов выполнить в одном процессе (тёплый режим агента)")
    parser.add_argument("--json", default=str(SPIKE_DIR / "results_prototype.json"))
    return parser.parse_args()


def capture_screen(camera=None) -> dict:
    """Слой «видел»: один кадр монитора в физических пикселях."""
    import dxcam

    owns = camera is None
    if owns:
        camera = dxcam.create(output_color="BGRA")
    try:
        started = time.perf_counter()
        frame = camera.grab()
        if frame is None:
            frame = camera.get_latest_frame()
        snapshot = frame.copy()
        elapsed_ms = (time.perf_counter() - started) * 1000.0
    finally:
        if owns:
            camera.release()
    return {"shape": list(snapshot.shape), "ms": round(elapsed_ms, 2), "frame": snapshot}


def find_element(uia, hwnd: int, name: str, control_type: str) -> tuple[object | None, dict]:
    """Слой «нашёл»: один адресный запрос по имени (+ тип) с кэш-реквестом."""
    conditions = [uia.CreatePropertyCondition(UIA.UIA_NamePropertyId, name)]
    if control_type == "button":
        conditions.append(uia.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId,
                                                      UIA.UIA_ButtonControlTypeId))
    condition = conditions[0] if len(conditions) == 1 else uia.CreateAndCondition(*conditions)

    # Область кэша ШИРЕ области поиска: иначе свойства найденного элемента не кэшируются
    # и чтение Cached* падает (см. RESULTS.md, раздел 1).
    cache = uia_common.make_cache(uia, UIA.TreeScope_Subtree, "action", False)
    started = time.perf_counter()
    element = uia.ElementFromHandle(hwnd).FindFirstBuildCache(UIA.TreeScope_Descendants, condition, cache)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if not element:
        return None, {"ms": round(elapsed_ms, 2), "found": False}
    node = uia_common.node_from_cached(element, "action")
    node["level"] = 0
    return element, {"ms": round(elapsed_ms, 2), "found": True, "node": node}


def click_element(user32, kernel32, hwnd: int, node: dict) -> dict:
    """Слой «кликнул»: активация окна, наведение, проверка курсора, нажатие."""
    rect = node["rect"]
    center = ((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
    screen = mc.metrics(user32)
    norm = mc.normalize(*center, screen)
    activated = mc.ensure_foreground(user32, kernel32, hwnd)
    time.sleep(0.15)
    outcome = mc.click_verified(user32, norm)
    return {"target_center": list(center), "normalized": list(norm),
            "window_activated": activated, "click": outcome}


def verify_click(click_log: Path, name: str, timeout: float = 1.0) -> dict:
    """Слой «проверил»: постусловие выполнено, если нажатие дошло до элемента."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        lines = click_log.read_text(encoding="utf-8").strip().splitlines()
        if lines and lines[-1].startswith(f"CLICK {name}"):
            return {"ok": True, "evidence": lines[-1]}
        time.sleep(0.02)
    lines = click_log.read_text(encoding="utf-8").strip().splitlines()
    return {"ok": False, "evidence": lines[-1] if lines else "<журнал пуст>"}


def run_cycles(args, uia, user32, kernel32, hwnd: int, camera) -> list[dict]:
    """Повторяет цикл «увидел → нашёл → кликнул → проверил» в одном процессе.

    Именно так будет работать агент: камера создаётся один раз, COM-объект UIA — один раз,
    а на каждом шаге остаётся только захват кадра, адресный поиск и клик.
    """
    import dxcam

    click_log = Path(args.click_log)
    cycles: list[dict] = []
    for index in range(max(1, args.repeat)):
        started = time.perf_counter()
        shot = capture_screen(camera)
        _, search = find_element(uia, hwnd, args.task, args.control_type)
        if not search["found"]:
            cycles.append({"index": index, "error": "элемент не найден"})
            break
        node = search["node"]
        outcome = click_element(user32, kernel32, hwnd, node)
        check = verify_click(click_log, args.task) if args.expect_click else {"ok": True, "evidence": "-"}
        cycles.append({
            "index": index,
            "ms_capture": shot["ms"],
            "ms_find": search["ms"],
            "ms_total": round((time.perf_counter() - started) * 1000.0, 1),
            "found": node["name"],
            "clicked": outcome["click"].get("landed"),
            "verified": check,
        })
    return cycles


def main() -> int:
    args = parse_args()
    dpi_mode = dpi_aware.enable()
    user32, _ = mc.prototypes()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(ctypes.c_ulong)]

    report: dict = {"dpi": dpi_mode, "task": args.task, "window_filter": args.window,
                    "connection_timeout_ms": CONNECTION_TIMEOUT_MS}
    total_started = time.perf_counter()

    # --- ВИДЕЛ
    shot = capture_screen()
    report["saw"] = {"frame_shape": shot["shape"], "ms": shot["ms"]}

    candidates = [window for window in uia_common.enum_windows() if args.window in window["title"]]
    if not candidates:
        report["error"] = f"окно с заголовком {args.window!r} не найдено"
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(report["error"])
        return 1
    window = candidates[0]
    hwnd = window["hwnd"]
    report["window"] = {**window, "hwnd": hex(hwnd), "rect": uia_common.window_rect(hwnd)}

    # --- НАШЁЛ
    Path(args.click_log).write_text("", encoding="utf-8")
    uia = uia_common.create_uia(CONNECTION_TIMEOUT_MS)
    try:
        _, search = find_element(uia, hwnd, args.task, args.control_type)
    except Exception as exc:  # noqa: BLE001 — зависший провайдер отдаёт 0x800705B4
        report["find"] = {"error": f"{type(exc).__name__}: {exc}", "fallback": "пиксельный путь"}
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"UIA не ответил ({type(exc).__name__}) — включается пиксельный фоллбэк")
        return 2
    report["found"] = search
    if not search["found"]:
        report["error"] = f"элемент {args.task!r} не найден"
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(report["error"])
        return 1

    node = search["node"]
    print(f"ВИДЕЛ: кадр {shot['shape']} за {shot['ms']} мс")
    print(f"НАШЁЛ: {node['control_type']} {node['name']!r} rect={node['rect']} за {search['ms']} мс")

    if args.dry_run:
        report["dry_run"] = True
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print("Dry-run: клик не выполняется")
        return 0

    # --- КЛИКНУЛ
    outcome = click_element(user32, kernel32, hwnd, node)
    report["clicked"] = outcome
    print(f"КЛИКНУЛ: центр {outcome['target_center']} -> норм {outcome['normalized']}, "
          f"курсор {outcome['click'].get('landed')}, окно активно: {outcome['window_activated']}")

    # --- ПРОВЕРИЛ
    if args.expect_click:
        check = verify_click(Path(args.click_log), args.task)
        report["verified"] = check
        print(f"ПРОВЕРИЛ: {'постусловие выполнено' if check['ok'] else 'ПОСТУСЛОВИЕ НЕ ВЫПОЛНЕНО'}"
              f" — {check['evidence']}")

    # --- Тёплый цикл: те же шаги без запуска процесса и создания камеры/COM заново
    if args.repeat > 1:
        import dxcam

        camera = dxcam.create(output_color="BGRA")
        try:
            cycles = run_cycles(args, uia, user32, kernel32, hwnd, camera)
        finally:
            camera.release()
        report["cycles"] = cycles
        warm = [cycle["ms_total"] for cycle in cycles if "ms_total" in cycle]
        if warm:
            print(f"Тёплый цикл ({len(warm)} повторов): медиана {sorted(warm)[len(warm) // 2]} мс, "
                  f"мин {min(warm)} мс, макс {max(warm)} мс")
        report["cycles_ok"] = sum(1 for cycle in cycles if cycle.get("verified", {}).get("ok"))

    report["total_ms"] = round((time.perf_counter() - total_started) * 1000.0, 1)
    Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Цикл целиком: {report['total_ms']} мс (результат: {args.json})")
    return 0 if report.get("verified", {"ok": True})["ok"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
