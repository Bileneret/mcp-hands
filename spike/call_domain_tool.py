"""Одноразовый вызов доменного инструмента через настоящий stdio-сервер
(воспроизводит стек хоста, где notepad_run падал с CO_E_NOTINITIALIZED).

Запуск: python -u mcp_hands_call_domain.py <инструмент>
"""
import json
import sys

import anyio

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

BASE = r"D:\BOT\MCP-Hands"


async def main() -> None:
    params = StdioServerParameters(
        command=BASE + r"\.venv\Scripts\python.exe",
        args=["-u", "-X", "utf8", "-m", "mcp_hands.mcp_server"],
        cwd="D:\\BOT",
        env={"SYSTEMROOT": __import__("os").environ["SYSTEMROOT"],
             "PYTHONPATH": BASE, "PYTHONUTF8": "1"})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(sys.argv[1], {})
            print(json.dumps(result.structured_content, ensure_ascii=False, indent=1))


anyio.run(main)
