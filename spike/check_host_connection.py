"""Разовая проверка подключения хоста: spawn сервера по конфигу из чужого cwd,
initialize + list_tools + health — как это сделает ZCode при старте сессии.
Запускать: python -u <путь>\check_host_connection.py
"""
import anyio
import json
import os

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> None:
    params = StdioServerParameters(
        command=r"D:\BOT\MCP-Hands\.venv\Scripts\python.exe",
        args=["-u", "-X", "utf8", "-m", "mcp_hands.mcp_server"],
        cwd="D:\\BOT",
        env={"SYSTEMROOT": os.environ["SYSTEMROOT"],
             "PYTHONPATH": r"D:\BOT\MCP-Hands", "PYTHONUTF8": "1"})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            health = await session.call_tool("health", {})
            print("инструментов:", len(tools.tools))
            print("health:", json.dumps(health.structured_content, ensure_ascii=False)[:220])


anyio.run(main)
