"""Замер захвата экрана: DXGI Desktop Duplication (dxcam) против mss.

Меряет то, что нужно планировщику: задержку одиночного кадра, поведение на статичном
экране (DXGI отдаёт кадр только при изменении), стоимость кропа окна и инициализации.
Кроп активного окна сохраняется в PNG — он же вход для теста локальной VLM.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dpi_aware  # noqa: E402
import uia_common  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Замер захвата экрана (DXGI против mss)")
    parser.add_argument("--frames", type=int, default=200, help="кадров на замер")
    parser.add_argument("--compare-mss", action="store_true", help="сравнить с mss")
    parser.add_argument("--window", default="Notepad.exe", help="окно для кропа (имя процесса)")
    parser.add_argument("--save-png", default="spike/.capture_full.png")
    parser.add_argument("--save-crop", default="spike/.capture_crop.png")
    parser.add_argument("--json", default=str(Path(__file__).resolve().parent / "results_dxgi.json"))
    return parser.parse_args()


def stats(samples: list[float]) -> dict:
    if not samples:
        return {"count": 0}
    ordered = sorted(samples)
    return {
        "count": len(samples),
        "ms_min": round(ordered[0], 3),
        "ms_median": round(statistics.median(ordered), 3),
        "ms_p95": round(ordered[int(len(ordered) * 0.95) - 1], 3),
        "ms_max": round(ordered[-1], 3),
    }


def bench_dxcam_grab(camera, frames: int) -> dict:
    """Одиночный grab: сколько стоит «дай текущий кадр» и как часто он отсутствует."""
    latencies: list[float] = []
    empty = 0
    for _ in range(frames):
        started = time.perf_counter()
        frame = camera.grab()
        elapsed = (time.perf_counter() - started) * 1000.0
        if frame is None:
            empty += 1
            continue
        latencies.append(elapsed)
    return {**stats(latencies), "empty_frames": empty, "shape": None}


def bench_dxcam_latest(camera, frames: int) -> dict:
    """get_latest_frame при РАБОТАЮЩЕМ захвате: копия буфера — так работает агент.

    Без `start()` этот вызов всегда возвращает None (буфера ещё нет), поэтому меряем
    при запущенном захвате и рядом кладём долю CPU — цену фонового потока.
    """
    camera.start(target_fps=120, video_mode=True)
    cpu_percent = 0.0
    try:
        time.sleep(0.3)  # прогрев буфера
        latencies: list[float] = []
        empty = 0
        for _ in range(frames):
            started = time.perf_counter()
            frame = camera.get_latest_frame()
            elapsed = (time.perf_counter() - started) * 1000.0
            if frame is None:
                empty += 1
                continue
            latencies.append(elapsed)
        cpu = time.process_time()
        time.sleep(1.0)
        cpu_percent = (time.process_time() - cpu) * 100.0
    finally:
        camera.stop()
    return {**stats(latencies), "empty_frames": empty,
            "cpu_percent_while_capturing": round(cpu_percent, 1)}


def bench_frame_copy(frame, repeats: int = 200) -> dict:
    """Снимок кадра в свою память: то, что агент платит за «дай мне картинку сейчас».

    Буфер dxcam переиспользуется, поэтому кадр обязательно копируется — иначе на следующем
    захвате пиксели изменятся под руками. `get_latest_frame` меряет не это, а ожидание
    следующего кадра при заданном target_fps.
    """
    latencies = []
    for _ in range(repeats):
        started = time.perf_counter()
        snapshot = frame.copy()
        latencies.append((time.perf_counter() - started) * 1000.0)
        del snapshot
    return stats(latencies)


def bench_dxcam_continuous(camera, seconds: float = 2.0) -> dict:
    """Непрерывный режим: сколько кадров в секунду реально отдаёт экран."""
    camera.start(target_fps=120, video_mode=True)
    try:
        time.sleep(seconds)
        seen = 0
        last = None
        started = time.perf_counter()
        while time.perf_counter() - started < seconds:
            frame = camera.get_latest_frame()
            if frame is None:
                continue
            if last is None or frame is not last:
                seen += 1
                last = frame
        elapsed = time.perf_counter() - started
    finally:
        camera.stop()
    return {"frames": seen, "seconds": round(elapsed, 2), "fps": round(seen / elapsed, 1)}


def bench_mss(frames: int) -> dict:
    """mss — то, чем захват делался в прошлом проекте: база для сравнения."""
    try:
        import mss
    except ImportError:
        return {"error": "mss не установлен"}

    latencies: list[float] = []
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        for _ in range(frames):
            started = time.perf_counter()
            shot = sct.grab(monitor)
            elapsed = (time.perf_counter() - started) * 1000.0
            latencies.append(elapsed)
    return stats(latencies)


def crop_cost(frame, rect: dict) -> tuple[dict, object]:
    """Кроп окна из готового кадра: срез numpy, то есть почти бесплатно."""
    latencies = []
    crop = None
    for _ in range(50):
        started = time.perf_counter()
        crop = frame[rect["top"]:rect["bottom"], rect["left"]:rect["right"]].copy()
        latencies.append((time.perf_counter() - started) * 1000.0)
    return stats(latencies), crop


def main() -> int:
    args = parse_args()
    dpi_mode = dpi_aware.enable()

    import dxcam
    from PIL import Image

    report: dict = {"dpi_awareness": dpi_mode, "monitor": dpi_aware.report()["monitor"]}

    # Нативный формат DXGI — BGRA: конвертацию делает срез numpy, cv2 не нужен вовсе.
    started = time.perf_counter()
    camera = dxcam.create(output_color="BGRA")
    report["ms_dxcam_create"] = round((time.perf_counter() - started) * 1000, 2)

    started = time.perf_counter()
    frame = camera.grab()
    report["ms_first_frame"] = round((time.perf_counter() - started) * 1000, 2)
    if frame is None:
        frame = camera.get_latest_frame()
    report["frame_shape"] = list(frame.shape)
    frame = frame.copy()  # своя память: у dxcam буфер переиспользуется
    camera.release()
    report["frame_copy"] = bench_frame_copy(frame)

    Image.fromarray(frame[:, :, 2::-1]).save(args.save_png)

    window = uia_common.find_windows(args.window)
    if window:
        rect = uia_common.window_rect(window[0]["hwnd"])
        rect = {key: max(0, value) for key, value in rect.items()}
        report["cropped_window"] = {**rect, "exe": window[0]["exe"], "title": window[0]["title"]}
        report["ms_crop"], crop = crop_cost(frame, rect)
        if crop is not None and crop.size:
            Image.fromarray(crop[:, :, 2::-1]).save(args.save_crop)
    else:
        report["cropped_window"] = None

    camera = dxcam.create(output_color="BGRA")
    report["grab"] = bench_dxcam_grab(camera, args.frames)
    report["latest_frame"] = bench_dxcam_latest(camera, args.frames)
    report["continuous"] = bench_dxcam_continuous(camera)
    camera.release()

    if args.compare_mss:
        report["mss"] = bench_mss(max(20, args.frames // 4))

    Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"DPI: {dpi_mode}; монитор {report['monitor']['dpi']} dpi "
          f"({report['monitor']['scale_percent']}%), кадр {report['frame_shape']}")
    print(f"инициализация dxcam: {report['ms_dxcam_create']} мс, первый кадр: {report['ms_first_frame']} мс")
    grab = report["grab"]
    print(f"grab: медиана {grab.get('ms_median')} мс, p95 {grab.get('ms_p95')} мс, "
          f"макс {grab.get('ms_max')} мс, пустых кадров {grab.get('empty_frames')} из {args.frames}")
    latest = report["latest_frame"]
    print(f"get_latest_frame: медиана {latest.get('ms_median')} мс, p95 {latest.get('ms_p95')} мс")
    print(f"непрерывный режим: {report['continuous']['fps']} кадр/с")
    copy_stats = report["frame_copy"]
    print(f"снимок кадра (копия {report['frame_shape'][1]}x{report['frame_shape'][0]}): "
          f"медиана {copy_stats['ms_median']} мс, p95 {copy_stats['ms_p95']} мс")
    print(f"кроп окна: медиана {report['ms_crop'].get('ms_median')} мс")
    print(f"CPU фонового захвата: {report['latest_frame'].get('cpu_percent_while_capturing')} % одного ядра")
    if "mss" in report:
        mss_stats = report["mss"]
        print(f"mss: медиана {mss_stats.get('ms_median')} мс, p95 {mss_stats.get('ms_p95')} мс "
              f"({mss_stats.get('count', 0)} кадров)")
    print(f"PNG: {args.save_png}" + (f", кроп: {args.save_crop}" if report["cropped_window"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
