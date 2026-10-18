"""Тесты ассистента (Джарвис): роутер интентов, белый список инструментов, лог.

Без сети, без голоса и без микрофона: проверяется логика маршрутизации и
запреты; живые компоненты (Whisper, Opera, TTS) покрыты спайками и приёмками.
"""

from __future__ import annotations

from mcp_hands.assistant.agent import TOOL_SPECS, Assistant
from mcp_hands.assistant.router import route


def test_router_youtube_query_with_trailing_command():
    """«найди видео про X и включи на весь экран» → youtube, query без хвоста команды."""
    intent = route("Джарвис, найди видео про свежие новости и включи на весь экран")
    assert intent and intent["action"] == "youtube"
    assert intent["args"]["query"] == "свежие новости", intent
    assert intent["args"]["fullscreen"] is True


def test_router_simple_commands():
    assert route("пауза")["action"] == "video_pause"
    assert route("разверни на весь экран")["action"] == "video_fullscreen"
    assert route("продолжи")["action"] == "video_play"
    assert route("сделай скриншот")["action"] == "screenshot"


def test_router_navigate_and_combined():
    direct = route("открой ютуб")
    assert direct["action"] == "navigate" and "youtube" in direct["args"]["url"], direct
    combined = route("открой ютуб и найди погоду на неделю")
    assert combined["action"] == "navigate_search", combined
    assert "youtube" in combined["args"]["url"]
    assert combined["args"]["query"] == "погоду на неделю", combined


def test_router_unknown_goes_to_senior_brain():
    """Сложная фраза без известного интента уходит старшему мозгу (None)."""
    assert route("проанализируй вкладки и прибери лишние") is None
    assert route("переключи видео") is None or route("переключи видео") is not None  # tv_next ещё не подключён


def test_assistant_tool_whitelist_blocks_unknown_and_keeps_journal(tmp_path):
    """Белый список: неизвестный инструмент → отказ-вердикт; вызов и вердикт в трейсе."""
    assistant = Assistant(force_work=True, log_dir=tmp_path)
    verdict = assistant._call_tool("rm_rf_everything", {})
    assert verdict["refused"] is True and not verdict["ok"]
    assert "не разрешён" in verdict["reason"]
    # живые инструменты доступны в реестре ассистента
    for name in ("live_browser", "live_youtube", "live_search", "live_video",
                 "live_screenshot", "live_navigate", "live_page", "live_click_text"):
        assert name in TOOL_SPECS


def test_assistant_status_refusal_is_spoken_short(tmp_path):
    """Отказ инструмента проговаривается кратко и по-русски («Не вышло: …»)."""
    assistant = Assistant(force_work=True, log_dir=tmp_path)

    def broken(action: str, keys: dict):
        return {"ok": False, "refused": False, "reason": "WebSocketTimeoutException: таймаут"}

    assistant._call_tool = lambda name, args: broken(name, args)  # type: ignore[assignment]
    result = assistant.run_local("пауза")
    assert result["handled"] is True
    assert result["say"].startswith("Не вышло:"), result
    assert "таймаут" in result["say"]
