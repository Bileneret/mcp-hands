"""Сквозные тесты MCP-сервера (in-memory транспорт: настоящий сервер, без пайпов).

Два слоя:
1. КОНТРАКТ без живого окружения: набор инструментов, отказы стражей и валидация
   аргументов возвращаются как нормальные вердикты (`refused`), а не исключения;
   до SendInput они не доходят по построению (проверяем и это — подменой актуатора).
2. ЖИВОЙ слой: поиск элемента и клик по нашей мишени — только на столе ввода
   (пропуск с причиной в изолированных сессиях, как у остальных живых тестов).
"""

from __future__ import annotations

import anyio
import pytest

from mcp import ClientSession
from mcp.client._memory import InMemoryTransport

import mcp_hands.mcp_server as ms

EXPECTED_TOOLS = {
    "windows_list", "element_find", "element_text", "content_map", "ocr_window",
    "window_activate", "click_node", "type_text", "hotkey",
    "notepad_run", "word_pdf_run", "browser_links_run", "health"}


def _call(coro):
    return anyio.run(coro)


async def _tools() -> list[str]:
    async with InMemoryTransport(ms.server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return [tool.name for tool in (await session.list_tools()).tools]


def test_tool_inventory():
    names = set(_call(_tools))
    missing = EXPECTED_TOOLS - names
    extra = names - EXPECTED_TOOLS
    assert not missing, f"нет заявленных инструментов: {missing}"
    assert not extra, f"незапланированные инструменты (стоп-лист!): {extra}"


async def _refusal_cases():
    async with InMemoryTransport(ms.server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            verdicts = {}
            verdicts["find_without_conditions"] = await session.call_tool(
                "element_find", {"hwnd": 123, "name": ""})
            verdicts["hotkey_unknown_key"] = await session.call_tool(
                "hotkey", {"hwnd": 123, "keys": ["WIN", "R"]})
            verdicts["hotkey_empty"] = await session.call_tool(
                "hotkey", {"hwnd": 123, "keys": []})
            verdicts["type_empty_text"] = await session.call_tool(
                "type_text", {"hwnd": 123, "text": ""})
            verdicts["click_headless_node"] = await session.call_tool(
                "click_node", {"hwnd": 123, "node": {"control_type": "button"}})
            return {name: (r.is_error, r.structured_content) for name, r in verdicts.items()}


def test_guard_refusals_are_results_not_errors():
    """Отказ стража/валидации — валидный типизированный вердикт, не протокол-ошибка."""
    cases = _call(_refusal_cases)
    for name, (is_error, structured) in cases.items():
        assert structured is not None, f"{name}: контракт ToolResult потерян"
        assert not is_error, f"{name}: отказ ушёл в протокол-ошибку"
        assert structured["ok"] is False, f"{name}: отказ должен давать ok=False"
        assert structured["refused"] is True, f"{name}: отказ обязан быть refused"
        assert structured["reason"], f"{name}: отказ без причины нечитаем"
    #windows_listБЕЗ клавиш ядра: до SendInput не дошло по построению.


def test_hoverkey_refusal_sends_nothing():
    """Отказ hotkey не создаёт даже актуатора: страж отсекает на уровне сервера."""
    import mcp_hands.mcp_server as ms_mod

    class Boom:
        def __getattr__(self, item):
            raise AssertionError(f"актуатор не должен затрагиваться ({item})")

    ms_mod._actuator = Boom()
    try:
        structured = _call(_refusal_cases)["hotkey_unknown_key"][1]
        assert structured["refused"] and not structured["ok"]
    finally:
        ms_mod._actuator = None


async def _health():
    async with InMemoryTransport(ms.server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("health", {})
            return result.structured_content


def test_health_live():
    data = _call(_health)
    assert data["ok"], data
    assert data["detail"].get("desktop"), "имя рабочего стола не получено"


async def _windows_list():
    async with InMemoryTransport(ms.server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("windows_list", {})
            return result.structured_content


def test_windows_list_live():
    data = _call(_windows_list)
    assert data["ok"], data
    assert "windows" in data["detail"], data


# --- живой слой: наша мишень -------------------------------------------------------


@pytest.fixture
def mcp_target(target, require_input_desktop):
    """Мишень, допущенная к вводу: стол тестов и мишени совпадают."""
    return target


async def _find_and_click_hwnd(hwnd: int, node_name: str):
    async with InMemoryTransport(ms.server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            activation = await session.call_tool("window_activate", {"hwnd": hwnd})
            if not activation.structured_content["ok"]:
                return {"activation": activation.structured_content}
            import time

            time.sleep(0.3)
            found = await session.call_tool("element_find", {
                "hwnd": hwnd, "name": node_name, "control_type": "button"})
            structured = found.structured_content
            if not structured["detail"].get("found"):
                return {"element_find": structured}
            node = structured["detail"]["node"]
            # rectangle в detail переименован: возвращаем настоящий узел из node в content...
            clicked = await session.call_tool("click_node", {"hwnd": hwnd, "node": node})
            return {"click": clicked.structured_content, "node": node}


def test_find_and_click_own_target(mcp_target):
    """Полный инструмент-цикл: активировать → найти → кликнуть; мишень фиксирует CLICK."""
    outcome = _call(lambda: _find_and_click_hwnd(mcp_target.hwnd, "alpha"))
    click = outcome.get("click")
    assert click is not None and click["ok"], outcome
    clicks = mcp_target.clicks()
    assert clicks, f"клик до мишени не дошёл: {clicks}"


def test_hotkey_refusal_parallel_session(mcp_target):
    """Страж горячих клавиш отказывает, если активно ЧУЖОЕ окно (не expect_hwnd):
    валидируем поведением, а не подменой — задача фокуса на мишень, verжидется иное."""
    async def case():
        async with InMemoryTransport(ms.server) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                # ожидаем окно, которого заведомо нет в фокусе — страж обязан отказать
                return await session.call_tool(
                    "hotkey", {"hwnd": mcp_target.hwnd + 4242,
                               "keys": ["CTRL", "S"]})

    structured = _call(case).structured_content
    assert not structured["ok"] and structured["refused"], structured
    assert "окно" in structured["reason"] or "активн" in structured["reason"], structured
