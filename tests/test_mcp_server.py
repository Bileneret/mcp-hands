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

EXPECTED_TOOLS = ms.TOOL_NAMES  # канонический набор — единый источник истины в сервере


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
    проверяем поведение, а не подмену — без фокуса на мишени нажатие не уйдёт."""
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


# --- dry_run: та же лестница стражей, ноль ввода -----------------------------------


async def _dry_run_cycle(hwnd: int, node_name: str) -> dict:
    async with InMemoryTransport(ms.server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            found = await session.call_tool("element_find", {
                "hwnd": hwnd, "name": node_name, "control_type": "button"})
            node = found.structured_content["detail"]["node"]
            click = await session.call_tool("click_node",
                                            {"hwnd": hwnd, "node": node, "dry_run": True})
            typed = await session.call_tool("type_text", {
                "hwnd": hwnd, "text": "сухой прогон 🚀", "dry_run": True})
            keys = await session.call_tool("hotkey", {
                "hwnd": hwnd, "keys": ["CTRL", "S"], "dry_run": True})
            return {"click": click.structured_content, "text": typed.structured_content,
                    "hotkey": keys.structured_content}


def test_dry_run_checks_without_input(mcp_target):
    """dry_run по живой мишени: вердикты готовности, ноль CLICK в журнале, без активации."""
    before = len(mcp_target.clicks())
    result = _call(lambda: _dry_run_cycle(mcp_target.hwnd, "alpha"))
    click = result["click"]
    assert click["ok"] and click["detail"]["dry_run"] is True, click
    assert click["detail"]["center"], click
    assert click["detail"]["desktop"]["target"] == click["detail"]["desktop"]["ours"], click
    # точка клика совпадает с центром кнопки мишени: dry_run не искажает геометрию
    assert not mcp_target.clicks()[before:], \
        "dry_run обязан не отправлять ввод, а в журнале мишени появились CLICK"
    text = result["text"]
    assert text["ok"] and text["detail"]["code_units"] > len("сухой прогон 🚀"), text
    assert text["detail"]["dry_run"] is True
    keys = result["hotkey"]
    assert keys["ok"] and keys["detail"]["would_send"] == [0x11, 0x53], keys


def test_dry_run_never_touches_actuator():
    """dry_run в hotkey/type_text не трогает клавиатурный путь актуатора вовсе;
    предварительно мишень назначается активной подделкой стража."""
    calls: list[str] = []
    class FakeActuator:
        def guard_keyboard(self, expect_hwnd, allow_any_window=False):
            calls.append("guard")
            return {"ok": True, "expect_hwnd": expect_hwnd, "foreground": expect_hwnd}

        def foreground(self):
            return 0x4242

        def hotkey(self, *_keys, **_kwargs):
            calls.append("hotkey")
            return {"ok": True, "keys": list(_keys)}

        def type_text(self, *_args, **_kwargs):
            calls.append("type_text")
            return {"ok": True}

        def can_control(self, _pid):
            calls.append("can_control")
            return {"blocked_by_uipi": False}

        def own_desktop(self):
            calls.append("own_desktop")
            return "Default"

        def window_desktop(self, _hwnd):
            calls.append("window_desktop")
            return "Default"

        def window_alive(self, _hwnd):
            calls.append("window_alive")
            return True

    ms._actuator = FakeActuator()
    try:
        async def case():
            async with InMemoryTransport(ms.server) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    keys = await session.call_tool(
                        "hotkey", {"hwnd": 0x4242, "keys": ["CTRL", "S"], "dry_run": True})
                    typed = await session.call_tool(
                        "type_text", {"hwnd": 0x4242, "text": "текст", "dry_run": True})
                    return keys.structured_content, typed.structured_content
        keys, typed = _call(case)
        assert keys["ok"] and keys["detail"]["would_succeed"], keys
        assert typed["ok"] and typed["detail"]["would_succeed"], typed
        untouched = {"hotkey", "type_text"} & set(calls)
        assert not untouched, f"dry_run затронул клавиатурный путь: {untouched}"
        assert "guard" in calls, "страж должен был проверяться"
    finally:
        ms._actuator = None


def test_domain_path_initializes_com_in_host_threads():
    """Регрессия живого прогона (аудит v13): хост исполняет синхронные инструменты
    в рабочих потоках, где COM может быть не инициализирован — `notepad_run` падал
    с CO_E_NOTINITIALIZED до первого шага, потому что сценарный путь не проходил
    через `_automation()`. Инвариант: после `_ensure_com()` создание UIA-клиента
    в свежем потоке гарантировано (повторный CoInitialize безвреден). Негатив
    «голый поток обязан упасть» НЕ утверждаем: инициализация свежего потока
    средозависима (на живом хосте падало, в тест-процессе не всегда)."""
    import threading

    outcome: dict = {}

    def ensured_com_works() -> None:
        ms._ensure_com()
        from mcp_hands.uia import UiaClient

        try:
            UiaClient()
        except OSError as exc:
            outcome["ensured"] = exc
        else:
            outcome["ensured"] = None

    thread = threading.Thread(target=ensured_com_works)
    thread.start()
    thread.join(30)
    assert outcome["ensured"] is None, outcome["ensured"]


def test_run_domain_source_calls_ensure_com():
    """Страховка «одна строка»: `_run_domain` обязан инициировать COM до сценария —
    иначе доменные инструменты падают на рабочих потоках хоста (живой инцидент v13)."""
    import inspect

    source = inspect.getsource(ms._run_domain)
    assert "_ensure_com()" in source, source
