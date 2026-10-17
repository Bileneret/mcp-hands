"""Спайк MCP (шаг 0 этапа 2): клиент-замерщик hello-сервера.

Замеряет: старт сервера и рукопожатие, list_tools, латентность вызовов (теплый
и холодный), кириллицу на весь круг (client -> server -> client). Результат —
в `results_mcp.json` и на stdout.

Запуск: python -m spike.mcp_hello_client
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

BASE = Path(__file__).resolve().parents[1]
PY = BASE / ".venv" / "Scripts" / "python.exe"

CYRILLIC = "Шаг за шагом: Мираж на кириллице «MCP-Hands» — 42%"


async def main() -> dict:
    params = StdioServerParameters(command=str(PY), args=["-u", "-m", "spike.mcp_hello_server"],
                                   cwd=str(BASE))
    report: dict = {"sdk": "mcp 2.3.0", "python": sys.version.split()[0]}
    started = time.perf_counter()
    async with stdio_client(params) as (read, write):
        report["spawn_s"] = round(time.perf_counter() - started, 3)
        async with ClientSession(read, write) as session:
            init_started = time.perf_counter()
            init = await session.initialize()
            report["init_s"] = round(time.perf_counter() - init_started, 3)
            report["server_name"] = init.server_info.name
            tools = await session.list_tools()
            report["tools"] = [t.name for t in tools.tools]
            # холодный вызов
            cold_started = time.perf_counter()
            pong = await session.call_tool("ping", {})
            report["call_cold_ms"] = round((time.perf_counter() - cold_started) * 1000, 1)
            report["ping_result"] = [{"type": c.type, "text": getattr(c, "text", None)}
                                     for c in pong.content]
            report["ping_structured"] = getattr(pong, "structured_content", None)
            # тёплые вызовы: латентность
            latencies = []
            for _ in range(10):
                started = time.perf_counter()
                await session.call_tool("ping", {})
                latencies.append((time.perf_counter() - started) * 1000)
            report["call_warm_ms"] = {
                "min": round(min(latencies), 1), "median": round(sorted(latencies)[5], 1),
                "max": round(max(latencies), 1)}
            # кириллица на весь круг
            echo = await session.call_tool("echo_cyrillic", {"text": CYRILLIC})
            block = echo.content[0]
            parsed = json.loads(block.text) if getattr(block, "text", None) else {}
            report["cyrillic"] = {
                "returned": parsed.get("echo", ""),
                "roundtrip_ok": parsed.get("echo") == CYRILLIC,
                "isError": bool(getattr(echo, "isError", False)),
                "structured": getattr(echo, "structuredContent", None)}
            # типизированный результат → structured_content (snake_case, SDK 2.x)
            typed = await session.call_tool("echo_model", {"text": CYRILLIC})
            report["typed"] = {
                "structured": getattr(typed, "structured_content", None),
                "isError": bool(getattr(typed, "is_error", False))}
    report["total_s"] = round(time.perf_counter() - started, 3)
    return report


if __name__ == "__main__":
    data = anyio.run(main)
    out = Path(__file__).resolve().parent / "results_mcp.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(data, ensure_ascii=False, indent=1))
