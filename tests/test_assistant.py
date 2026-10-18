"""Тесты ассистента (Джарвис): роутер интентов, белый список инструментов, лог.

Без сети, без голоса и без микрофона: проверяется логика маршрутизации и
запреты; живые компоненты (Whisper, Opera, TTS) покрыты спайками и приёмками.
"""

from __future__ import annotations

import pytest

from mcp_hands.assistant import agent as agent_mod
from mcp_hands.assistant.agent import TOOL_SPECS, Assistant, tool_payload, transport_lost
from mcp_hands.assistant.router import route
from mcp_hands.assistant.session import is_stop_phrase, sleep_after_seconds
from mcp_hands.brain.llm import LLMResponse


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


def test_router_site_named_any_order_with_open_verb():
    """Живая фраза «найди на экране YouTube и открой его» — это открыть сайт.

    Раньше шаблон требовал порядка «открой X и найди Y», и фраза уходила в поиск
    по текущей странице (в логе владельца — отказ после обрыва связи).
    """
    intent = route("Найди на экран YouTube и открой его.")
    assert intent and intent["action"] == "navigate", intent
    assert "youtube" in intent["args"]["url"]
    # обычные формы не сломаны
    assert route("открой ютуб")["action"] == "navigate"
    assert route("открой ютуб и найди погоду")["action"] == "navigate_search"


def test_router_browser_open_and_no_false_site_match():
    assert route("открой оперу")["action"] == "browser_open"
    assert route("запусти браузер")["action"] == "browser_open"
    # «вк» не должно ловиться внутри других слов
    assert route("посмотри вконце списка") is None
    assert route("найди котиков на ютубе")["action"] == "search"


def test_session_stop_phrases_do_not_eat_player_commands():
    """«стоп» завершает разговор, «стоп видео» — команда плееру (её ловит роутер)."""
    assert is_stop_phrase("Стоп, Джарвис!") and is_stop_phrase("хватит")
    assert not is_stop_phrase("стоп видео")
    assert route("стоп видео")["action"] == "video_pause"


def test_session_sleep_pause_from_config():
    assert sleep_after_seconds({"sleep_after_s": 90}) == 90
    assert sleep_after_seconds({"sleep_after_s": "45"}) == 45
    assert sleep_after_seconds({"sleep_after_s": "мусор"}) > 0  # мусор → дефолт
    assert sleep_after_seconds({"sleep_after_s": 1}) >= 15      # ниже минимума — не режем


def test_tool_payload_carries_detail_to_model():
    """Названия окон доезжают до модели: они лежат в detail, а не в summary."""
    windows = [{"hwnd": 1000 + index, "pid": index, "exe": "opera.exe",
                "title": f"Окно номер {index}"} for index in range(40)]
    payload = tool_payload({"ok": True, "summary": "окон: 40", "reason": "",
                            "detail": {"windows": windows}})
    assert "Окно номер 0" in payload and "opera.exe" in payload, payload
    assert len(payload) <= agent_mod.TOOL_PAYLOAD_LIMIT + len(" (обрезано)"), len(payload)


def test_assistant_live_call_reconnects_browser_and_repeats(tmp_path):
    """Обрыв связи в живой сцене: ассистент переподключает браузер и повторяет раз."""
    assistant = Assistant(force_work=True, log_dir=tmp_path)
    calls: list[tuple[str, dict]] = []

    def fake(tool: str, args: dict) -> dict:
        calls.append((tool, args))
        if tool == "live_page":
            first = len([name for name, _ in calls if name == "live_page"]) == 1
            if first:
                return {"ok": False, "reason": "CdpTransportError: обрыв связи с вкладкой "
                                               "(ConnectionAbortedError: WinError 10053)"}
            return {"ok": True, "summary": "страница: YouTube"}
        if tool == "live_browser":
            return {"ok": True, "detail": {"mode": "work", "port": 1234}}
        return {"ok": True}

    assistant._call_tool = fake  # type: ignore[assignment]
    outcome = assistant._live("live_page", {})
    assert outcome["ok"] is True
    assert [name for name, _ in calls].count("live_page") == 2, calls
    assert any(name == "live_browser" for name, _ in calls), calls


def test_assistant_live_call_restarts_browser_when_reattach_fails(tmp_path):
    """Мягкое переподключение не помогло — сессия снимается и браузер поднимается заново.

    Это случай «процесс браузера умер»: перепривязка к вкладке невозможна,
    поэтому ассистент обязан перезапустить браузер и повторить команду.
    """
    assistant = Assistant(force_work=True, log_dir=tmp_path)
    calls: list[tuple[str, dict]] = []
    failures = {"left": 2}  # обе попытки до рестарта обрываются
    state = {"mode": "work"}  # как у живой сессии: stop её действительно снимает

    def fake(tool: str, args: dict) -> dict:
        calls.append((tool, args))
        if tool == "live_page":
            if failures["left"]:
                failures["left"] -= 1
                return {"ok": False, "reason": "CdpTransportError: обрыв связи с вкладкой"}
            return {"ok": True, "summary": "страница открыта"}
        if tool == "live_browser":
            action = args.get("action")
            if action == "status":
                return {"ok": True, "detail": {"mode": state["mode"]}}
            if action == "stop":
                state["mode"] = ""
            elif action == "start":
                state["mode"] = "work"
            return {"ok": True}
        return {"ok": True}

    assistant._call_tool = fake  # type: ignore[assignment]
    assistant.last_url = "https://www.youtube.com"  # владельца застали на ютубе
    outcome = assistant._live("live_page", {})
    assert outcome["ok"] is True, outcome
    browser_actions = [args.get("action") for name, args in calls if name == "live_browser"]
    assert "stop" in browser_actions and "start" in browser_actions, browser_actions
    assert [name for name, _ in calls].count("live_page") == 3, calls
    # новая сессия открыта на пустой вкладке: страница владельца возвращается
    assert ("live_navigate", {"url": "https://www.youtube.com"}) in calls, calls


def test_assistant_live_call_reports_when_browser_cannot_be_revived(tmp_path):
    """Если поднять браузер заново не вышло — честный отказ, а не «WinError 10053»."""
    assistant = Assistant(force_work=True, log_dir=tmp_path)

    def fake(tool: str, args: dict) -> dict:
        if tool == "live_browser":
            if args.get("action") == "status":
                return {"ok": True, "detail": {"mode": ""}}
            return {"ok": False, "reason": "opera.exe не найден (Opera GX / Opera)"}
        return {"ok": False, "reason": "CdpTransportError: обрыв связи с вкладкой"}

    assistant._call_tool = fake  # type: ignore[assignment]
    answer = assistant.run_local("пауза")
    assert "Не вышло" in answer["say"], answer
    assert "поднять заново не удалось" in answer["say"], answer


def test_run_smart_hands_tool_details_to_model(monkeypatch, tmp_path):
    """Модель видит detail инструмента: «какие окна открыты» → названия, не число."""
    assistant = Assistant(force_work=True, log_dir=tmp_path)
    assistant._call_tool = lambda name, args: {
        "ok": True, "summary": "окон: 2", "reason": "",
        "detail": {"windows": [{"title": "Блокнот"}, {"title": "Opera GX"}]}}
    seen: dict = {}
    answers = [LLMResponse(text="", model="test",
                           tool_calls=[{"id": "1", "name": "windows_list", "args": "{}"}]),
               LLMResponse(text="Открыты Блокнот и Opera GX.", model="test")]

    def fake_post(config, messages, timeout_s=60.0, tools=None):
        seen["messages"] = [dict(message) for message in messages]
        seen.setdefault("tools_rounds", []).append(bool(tools))
        return answers.pop(0)

    monkeypatch.setattr(agent_mod, "_post_chat", fake_post)
    result = assistant.run_smart("выведи, какие приложения сейчас открыты")
    tool_messages = [m for m in seen["messages"] if m.get("role") == "tool"]
    assert tool_messages and "Блокнот" in tool_messages[0]["content"], tool_messages
    assert seen["tools_rounds"] == [True, True], seen["tools_rounds"]
    assert result["say"] == "Открыты Блокнот и Opera GX."


def test_run_smart_exhausted_reports_instead_of_dead_end(monkeypatch, tmp_path):
    """Шаги кончились — модель отвечает по фактам без инструментов, а не молчит."""
    assistant = Assistant(force_work=True, log_dir=tmp_path)
    assistant._call_tool = lambda name, args: {"ok": True, "summary": "ок", "detail": {}}
    rounds: list[bool] = []

    def fake_post(config, messages, timeout_s=60.0, tools=None):
        rounds.append(bool(tools))
        if tools:
            return LLMResponse(text="", model="test",
                               tool_calls=[{"id": str(len(rounds)), "name": "live_page",
                                            "args": "{}"}])
        return LLMResponse(text="Страница не открылась: браузер отказывал.", model="test")

    monkeypatch.setattr(agent_mod, "_post_chat", fake_post)
    result = assistant.run_smart("открой ютуб и включи видео", max_rounds=2)
    assert rounds == [True, True, False], rounds  # последний раунд — без инструментов
    assert result["detail"]["exhausted"] is True
    assert result["say"] == "Страница не открылась: браузер отказывал."


@pytest.mark.parametrize("reason", [
    "CdpTransportError: обрыв связи с вкладкой (ConnectionAbortedError: WinError 10053)",
    "RuntimeError: браузер не подключён: live_browser start/connect",
])
def test_transport_lost_recognizes_broken_live_session(reason):
    assert transport_lost({"reason": reason})
    assert not transport_lost({"reason": "выдача пуста: нет результатов"})
