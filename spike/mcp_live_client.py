"""Приёмка MCP-сервера живым stdio-клиентом (этап 2, шаг 1).

Отличие от тестов: сервер поднимается в ОТДЕЛЬНОМ процессе настоящим stdio-транспортом
(как его поднимет MCP-хост), а не in-memory. Цикл приёмки × 5: активировать окно
мишени → найти кнопку → кликнуть → прочитать текст → OCR; после каждого цикла сверяем
журнал кликов мишени — клик обязан дойти ровно один раз.

Запуск: python -m spike.mcp_live_client
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

BASE = Path(__file__).resolve().parents[1]
PY = BASE / ".venv" / "Scripts" / "python.exe"
CYCLES = 5


def spawn_target(log_path: Path) -> tuple[subprocess.Popen, int]:
    command = [str(PY), "-u", "-m", "mcp_hands.testing.fixture_window",
               "--title", "MCP-Hands live accept", "--click-log", str(log_path)]
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace")
    while True:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("мишень не поднялась")
        if line.startswith("HWND="):
            return proc, int(line.strip().split("=", 1)[1], 16)


def clicks_count(log_path: Path) -> int:
    return len([line for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.startswith("CLICK")])


async def one_cycle(session: ClientSession, hwnd: int) -> dict:
    started = time.perf_counter()
    steps = {}
    activation = await session.call_tool("window_activate", {"hwnd": hwnd})
    steps["activate"] = activation.structured_content["ok"]
    if not steps["activate"]:
        return {"ok": False, "steps": steps, "reason": "активация"}
    await anyio.sleep(0.35)
    found = await session.call_tool("element_find", {
        "hwnd": hwnd, "name": "alpha", "control_type": "button"})
    steps["find"] = found.structured_content["detail"].get("found") is True
    if not steps["find"]:
        return {"ok": False, "steps": steps, "reason": "поиск кнопки"}
    node = found.structured_content["detail"]["node"]
    clicked = await session.call_tool("click_node", {"hwnd": hwnd, "node": node})
    steps["click"] = clicked.structured_content["ok"]
    if not steps["click"]:
        return {"ok": False, "steps": steps,
                "reason": clicked.structured_content["reason"], "steps_detail": steps}
    text_window = await session.call_tool("element_text", {"hwnd": hwnd})
    steps["text_read"] = text_window.structured_content["ok"]
    ocr = await session.call_tool("ocr_window", {"hwnd": hwnd})
    steps["ocr"] = ocr.structured_content["ok"]
    return {"ok": all(steps.values()), "steps": steps,
            "ms": round((time.perf_counter() - started) * 1000, 1)}


async def main() -> dict:
    report: dict = {"transport": "stdio (отдельный процесс)", "cycles": []}
    params = StdioServerParameters(command=str(PY), args=["-u", "-m", "mcp_hands.mcp_server"],
                                   cwd=str(BASE))
    target_proc = None
    try:
        log_dir = BASE / "logs" / "mcp_accept"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "live_accept.log"
        log_path.write_text("", encoding="utf-8")
        target_proc, hwnd = spawn_target(log_path)
        started = time.perf_counter()
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                report["init_s"] = round(time.perf_counter() - started, 3)
                health = await session.call_tool("health", {})
                report["health"] = health.structured_content["ok"]
                total_before = clicks_count(log_path)
                for cycle in range(1, CYCLES + 1):
                    outcome = await one_cycle(session, hwnd)
                    outcome["cycle"] = cycle
                    # ровно один новый клик в журнале мишени за цикл — инвариант доставки
                    outcome["one_click_landed"] = \
                        clicks_count(log_path) - total_before == cycle
                    report["cycles"].append(outcome)
    finally:
        if target_proc is not None:
            target_proc.kill()
            target_proc.communicate()
    return report


if __name__ == "__main__":
    data = anyio.run(main)
    out = Path(__file__).resolve().parent / "results_mcp_live.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(data, ensure_ascii=False, indent=1))
    passed = sum(1 for c in data["cycles"] if c["ok"] and c["one_click_landed"])
    print(f"ИТОГ: {passed}/{len(data['cycles'])} циклов зелёные")
