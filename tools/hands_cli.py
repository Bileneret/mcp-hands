"""Пульт MCP-Hands: интерактивный CLI поверх настоящего MCP-сервера (stdio).

Пользователь сам вызывает инструменты и видит структурированные вердикты —
тот же путь, которым ходит любой MCP-хост, но без посредника: удобно для
приёмки «руками» и для отладки.

Запуск:
    .venv\\Scripts\\python.exe -u tools\\hands_cli.py

Команды:
    health                     — проверка рук (стол, DPI, браузерные/офисные пути)
    tools                      — список инструментов сервера
    call <имя> <JSON-аргументы> — вызов инструмента, например:
        call windows_list {}
        call element_find {"hwnd": 123456, "name": "Файл"}
        call notepad_run {}
    demo                        — безопасный набор только для чтения
    help / quit
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import anyio  # noqa: E402
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "Scripts" / "python.exe"

HELP = """\
Команды:
  health                        проверить руки (рабочий стол, DPI)
  tools                         список всех инструментов
  call <имя> <аргументы-JSON>   вызов инструмента (пример: call notepad_run {})
  demo                          безопасный тур только для чтения
  help                          эта справка
  quit                          выход
"""


def show(result) -> None:
    """Печатает вердикт инструмента: контракт ok/refused/reason/detail."""
    structured = getattr(result, "structured_content", None)
    if structured is None:
        for block in result.content:
            text = getattr(block, "text", "")
            if text:
                print(text)
        return
    mark = "OK" if structured.get("ok") else ("ОТКАЗ" if structured.get("refused") else "ОШИБКА")
    print(f"[{mark}] {structured.get('summary', '')}")
    if structured.get("reason"):
        print(f"  причина: {structured['reason']}")
    detail = structured.get("detail") or {}
    trimmed = json.dumps(detail, ensure_ascii=False, indent=1)
    if len(trimmed) > 3000:
        trimmed = trimmed[:3000] + "\n  … (сокращено)"
    if trimmed not in ("{}", "null"):
        print(trimmed)


async def session_loop() -> None:
    params = StdioServerParameters(
        command=str(PY), args=["-u", "-X", "utf8", "-m", "mcp_hands.mcp_server"],
        cwd=str(ROOT),
        env={"SYSTEMROOT": __import__("os").environ["SYSTEMROOT"],
             "PYTHONPATH": str(ROOT), "PYTHONUTF8": "1"})
    # stderr серверного процесса (INFO-логи comtypes и пр.) — в файл: терминал
    # пульта остаётся чистым, а диагностика сохраняется
    log_path = ROOT / "logs" / "mcp_server_stderr.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await run_commands(session, log_path)


async def run_commands(session, log_path) -> None:
    init = await session.initialize()
    print(f"Подключено: {init.server_info.name} — сервер MCP-Hands.")
    print(f"Журнал сервера: {log_path}")
    print("Введите help для списка команд.\n")
    while True:
        try:
            raw = await anyio.to_thread.run_sync(lambda: input("mcp-hands> "))
        except (EOFError, KeyboardInterrupt):
            print()
            return
        line = raw.strip()
        if not line:
            continue
        name, _, rest = line.partition(" ")
        name = name.lower()
        if name in ("quit", "exit", "q"):
            return
        if name == "help":
            print(HELP)
            continue
        if name == "tools":
            listing = await session.list_tools()
            for tool in listing.tools:
                first_line = (tool.description or "").splitlines()[0]
                print(f"  {tool.name:<26} {first_line[:90]}")
            continue
        if name == "health":
            show(await session.call_tool("health", {}))
            continue
        if name == "demo":
            print("— health:")
            show(await session.call_tool("health", {}))
            print("— windows_list (первые 5 окон):")
            result = await session.call_tool("windows_list", {})
            structured = getattr(result, "structured_content", None) or {}
            windows = (structured.get("detail") or {}).get("windows", [])[:5]
            for window in windows:
                print(f"    hwnd={window.get('hwnd')} «{window.get('title', '')[:60]}»")
            continue
        if name == "call":
            target, _, args_text = rest.partition(" ")
            if not target:
                print("формат: call <имя> <JSON>, например: call notepad_run {}")
                continue
            try:
                arguments = json.loads(args_text) if args_text.strip() else {}
            except json.JSONDecodeError as exc:
                print(f"аргументы не JSON: {exc}")
                continue
            result = await session.call_tool(target, arguments)
            show(result)
            continue
        print(f"неизвестная команда {name!r}; введите help")


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — старая консоль: печать пойдёт как есть
        pass
    anyio.run(session_loop)


if __name__ == "__main__":
    main()
