"""Спайк MCP (шаг 0 этапа 2): hello-сервер stdio.

Два инструмента: `ping` (рука-рукопожатие с замером) и `echo_cyrillic`
(проверка кодировок — именно на кириллице JSON-RPC по stdio на Windows ломается,
если где-то в цепочке включена OEM-кодировка консоли).
Запуск: python -m spike.mcp_hello_server
"""

from __future__ import annotations

import anyio
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

server = MCPServer(name="mcp-hands-spike", title="MCP-Hands spike",
                   instructions="Спайковый сервер этапа 2: проверка транспорта и кодировок.",
                   version="0.1.0")


@server.tool()
def ping() -> dict:
    """Возвращает pong — здоровается с клиентом без побочных эффектов."""
    return {"ok": True, "reply": "pong"}


@server.tool()
def echo_cyrillic(text: str) -> dict:
    """Возвращает переданный текст как есть — проверка UTF-8 на весь круг."""
    return {"ok": True, "echo": text, "length": len(text)}


class EchoResult(BaseModel):
    ok: bool
    echo: str
    guard_refused: bool = False


@server.tool(structured_output=True)
def echo_model(text: str) -> EchoResult:
    """Тот же эхо, но с типизированным результатом — проверка structuredContent."""
    return EchoResult(ok=True, echo=text)


if __name__ == "__main__":
    anyio.run(server.run_stdio_async)
