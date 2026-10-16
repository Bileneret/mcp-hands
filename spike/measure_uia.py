"""Оркестратор замеров UIA-зонда: повторы, сравнение режимов, проверка таймаута 500 мс.

Родитель запускает зонд дочерним процессом (`python -u spike/uia_probe.py`), потому что зонд,
висящий в COM-вызове, изнутри процесса не прервать — только убить процесс целиком.
Это тот механизм, который в DESIGN.md §4.2 называется «процесс-зонд + IPC-таймаут 500 мс».
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

SPIKE_DIR = Path(__file__).resolve().parent
PROBE = SPIKE_DIR / "uia_probe.py"
FIXTURE = SPIKE_DIR / "hang_window.py"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Замеры UIA-зонда")
    parser.add_argument("--exe", action="append", default=[],
                        help="цель замера (можно несколько раз); по умолчанию — стенд по умолчанию")
    parser.add_argument("--depth", type=int, default=3)
    parser.add_argument("--warmup", type=int, default=1, help="прогревающих прогонов перед замерами")
    parser.add_argument("--runs", type=int, default=3, help="измеряемых прогонов")
    parser.add_argument("--modes", default="cached,perparent,naive,descendants")
    parser.add_argument("--limit", type=int, default=400)
    parser.add_argument("--jump", choices=("none", "auto", "document", "render_widget"), default="none")
    parser.add_argument("--hang-budget-ms", type=int, default=500,
                        help="бюджет процесс-зонда на зависшем окне (kill после него)")
    parser.add_argument("--hang-raw-cap-s", type=float, default=20.0,
                        help="сколько ждать ответа зависшего окна без бюджета")
    parser.add_argument("--skip-hang", action="store_true", help="не проверять зависшее окно")
    parser.add_argument("--only-hang", action="store_true", help="только проверка зависшего окна")
    parser.add_argument("--json", default=str(SPIKE_DIR / "results_uia.json"))
    return parser.parse_args()


# --- запуск зонда -----------------------------------------------------------


def run_probe(target_arg: list[str], mode: str, depth: int, limit: int,
              timeout_s: float, extra: list[str] | None = None,
              json_path: Path | None = None, jump: str = "none") -> dict:
    """Запускает зонд дочерним процессом. Возвращает статус, время и разобранный JSON."""
    command = [sys.executable, "-u", str(PROBE), *target_arg,
               "--mode", mode, "--depth", str(depth), "--limit", str(limit)]
    if json_path is not None:
        command += ["--json", str(json_path)]
    if jump != "none":
        command += ["--jump", jump]
    command += extra or []

    started = time.perf_counter()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace",
                               creationflags=CREATE_NO_WINDOW)
    killed = False
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        killed = True
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    payload = None
    if json_path is not None and json_path.exists():
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        json_path.unlink()

    return {
        "killed": killed,
        "wall_ms": round(elapsed_ms, 1),
        "returncode": process.returncode,
        "stdout_tail": (stdout or "")[-400:],
        "stderr_tail": (stderr or "")[-400:],
        "payload": payload,
    }


def summarize(runs: list[dict], mode: str) -> dict:
    """Сводка по серии одинаковых прогонов: медиана, минимум, максимум."""
    usable = [run for run in runs if run["payload"]]
    if not usable:
        return {"mode": mode, "runs": 0, "failed": len(runs),
                "errors": [run["stderr_tail"] for run in runs if run["stderr_tail"]][:1]}
    totals = [run["payload"]["ms_total"] for run in usable]
    elements = [run["payload"]["total_elements"] for run in usable]
    return {
        "mode": mode,
        "runs": len(usable),
        "elements": int(statistics.median(elements)),
        "ms_min": round(min(totals), 1),
        "ms_median": round(statistics.median(totals), 1),
        "ms_max": round(max(totals), 1),
        "ms_per_element": round(statistics.median(totals) / max(1, statistics.median(elements)), 3),
        "levels": usable[-1]["payload"]["levels"],
        "nodes_dumped": usable[-1]["payload"]["nodes_dumped"],
        "sample_nodes": usable[-1]["payload"]["nodes"][:12],
    }


# --- проверка зависшего окна ------------------------------------------------


def hang_test(budget_ms: int, raw_cap_s: float, timeouts=(200, 500, 1000)) -> dict:
    """Сравнивает защиты на неотвечающем окне: без защиты, процесс-бюджет, ConnectionTimeout."""
    fixture = subprocess.Popen(
        [sys.executable, "-u", str(FIXTURE), "--responsive-sec", "0.4", "--freeze-sec", "90"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace", creationflags=CREATE_NO_WINDOW)
    result: dict = {"fixture_pid": fixture.pid}
    try:
        hwnd = None
        deadline = time.time() + 15
        while time.time() < deadline:
            line = fixture.stdout.readline()
            if not line:
                break
            if line.startswith("HWND="):
                hwnd = line.strip().split("=", 1)[1]
            if line.startswith("FREEZE"):
                break
        if not hwnd:
            return {**result, "error": "фикстура не сообщила HWND"}
        result["hwnd"] = hwnd
        target = ["--hwnd", hwnd]

        # 1. Процесс-зонд с бюджетом: ровно то, что заложено в DESIGN §4.2.
        result["with_budget"] = run_probe(target, "cached", 3, 50, budget_ms / 1000.0,
                                          json_path=SPIKE_DIR / ".hang_budget.json")

        # 2. Без бюджета и без ConnectionTimeout — сколько реально висит вызов.
        result["no_guard"] = run_probe(target, "cached", 3, 50, raw_cap_s,
                                       json_path=SPIKE_DIR / ".hang_raw.json")

        # 3. Штатный таймаут UIA (IUIAutomation2.ConnectionTimeout) — внутренняя защита.
        result["connection_timeout"] = {
            str(ms): run_probe(target, "cached", 3, 50, raw_cap_s,
                               extra=["--connection-timeout-ms", str(ms)],
                               json_path=SPIKE_DIR / f".hang_conn_{ms}.json")
            for ms in timeouts
        }
        return result
    finally:
        fixture.kill()
        fixture.communicate()


def _hresult(outcome: dict) -> str:
    """Код ошибки из stderr зонда: у таймаута UIA это 0x800705B4 (ERROR_TIMEOUT)."""
    tail = outcome.get("stderr_tail") or ""
    marker = "_ctypes.COMError: (-"
    if marker in tail:
        return tail.split(marker, 1)[1].split(",", 1)[0].strip()
    return ""


# --- основной сценарий ------------------------------------------------------


def main() -> int:
    args = parse_args()
    targets = args.exe or ["Notepad.exe", "opera.exe", "Antigravity.exe"]
    modes = [mode.strip() for mode in args.modes.split(",") if mode.strip()]

    report: dict = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "depth": args.depth,
        "jump": args.jump,
        "targets": {},
        "hang": None,
    }

    for exe in ([] if args.only_hang else targets):
        print(f"\n=== {exe} ===")
        target_report: dict = {}
        for _ in range(max(0, args.warmup)):
            run_probe(["--exe", exe], "cached", args.depth, args.limit, 60,
                      json_path=SPIKE_DIR / ".warmup.json", jump=args.jump)

        for mode in modes:
            runs = []
            for index in range(args.runs):
                outcome = run_probe(["--exe", exe], mode, args.depth, args.limit, 180,
                                    json_path=SPIKE_DIR / ".measure_run.json", jump=args.jump)
                runs.append(outcome)
                if outcome["killed"]:
                    print(f"  {mode} #{index + 1}: УБИТ по таймауту ({outcome['wall_ms']} мс)")
            summary = summarize(runs, mode)
            target_report[mode] = summary
            if summary.get("runs"):
                print("  {:<12} узлов {:>5}  медиана {:>8} мс  (мин {:>7}, макс {:>7})  {:>7} мс/узел".format(
                    mode, summary["elements"], summary["ms_median"],
                    summary["ms_min"], summary["ms_max"], summary["ms_per_element"]))
                for level in summary["levels"]:
                    print("      уровень {:<3} родителей {:>4}  элементов {:>5}  {:>8} мс".format(
                        level["level"], level["parents"], level["elements"], level["ms"]))
            else:
                print(f"  {mode}: прогоны не удались — {summary.get('errors', ['?'])}")
        report["targets"][exe] = target_report

    if not args.skip_hang:
        print("\n=== зависшее окно (фикстура) ===")
        hang = hang_test(args.hang_budget_ms, args.hang_raw_cap_s)
        report["hang"] = hang
        budget = hang.get("with_budget") or {}
        raw = hang.get("no_guard") or {}
        print(f"  бюджет {args.hang_budget_ms} мс (kill)     : "
              f"{'УБИТ' if budget.get('killed') else 'вернулся'} за {budget.get('wall_ms')} мс")
        print(f"  без защиты              : "
              f"{'УБИТ' if raw.get('killed') else 'вернулся'} за {raw.get('wall_ms')} мс "
              f"(кап {args.hang_raw_cap_s} с)")
        for ms, outcome in (hang.get("connection_timeout") or {}).items():
            payload = outcome.get("payload") or {}
            print(f"  ConnectionTimeout {ms:>4} мс: "
                  f"{'УБИТ' if outcome.get('killed') else 'прерван'} — процесс {outcome.get('wall_ms')} мс, "
                  f"сам вызов {payload.get('ms_walk', '?')} мс, код {_hresult(outcome)}")

    Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nРезультат: {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
