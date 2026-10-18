"""Тесты голосового слоя ассистента: конфиг голоса, микрофон, диалог, каталог.

Без звука и без микрофона: проверяются выбор/сохранение, список устройств,
история разговора и полнота каталога инструментов (31 из канона).
"""

from __future__ import annotations

import json

from mcp_hands.assistant import mic
from mcp_hands.assistant.dialogue import Dialogue
from mcp_hands.assistant.agent import TOOL_SPECS, Assistant, _build_registry
from mcp_hands.mcp_server import TOOL_NAMES


def test_tts_choice_defaults_and_save_keeps_llm_section(tmp_path, monkeypatch):
    """Сохранение голоса не трогает секцию llm (ключи провайдера остаются)."""
    from mcp_hands.assistant import tts

    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({
        "llm": {"base_url": "https://api.example", "api_key": "rv__SECRET", "model": "m"},
        "assistant": {"mic": {"device": "Headset"}}}), encoding="utf-8")
    monkeypatch.setattr(tts, "CONFIG_PATH", config)

    tts.save_config({"tts": {"provider": "edge", "voice": "ru-RU-SvetlanaNeural",
                             "rate": "+15%"}})
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["llm"]["api_key"] == "rv__SECRET"          # не потревожено
    assert data["assistant"]["mic"]["device"] == "Headset"  # не потревожено
    assert data["assistant"]["tts"]["voice"] == "ru-RU-SvetlanaNeural"
    choice = tts.current_choice()
    assert choice["provider"] == "edge" and choice["rate"] == "+15%"


def test_mic_devices_and_resolution_shape():
    """Список устройств и разрешение выбора не падают и дают понятную форму."""
    devices = mic.list_input_devices()
    assert isinstance(devices, list)
    for device in devices:
        assert {"index", "name", "channels", "is_default"} <= set(device)
    resolved, note = mic.resolve_device()
    assert resolved is None or isinstance(resolved, int)
    assert isinstance(note, str) and note


def test_dialogue_remembers_and_clears(tmp_path):
    dialogue = Dialogue(tmp_path / "память.db")
    dialogue.remember("user", "включи новости")
    dialogue.remember("assistant", "Включаю свежие новости.", kind="local")
    history = dialogue.history(limit=8)
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[-1]["content"].startswith("Включаю")
    assert dialogue.clear() == 2 and dialogue.history() == []
    dialogue.close()


def test_assistant_catalog_covers_full_canon_with_typed_schemas(tmp_path):
    _build_registry()
    assert set(TOOL_SPECS) == set(TOOL_NAMES), "каталог ассистента должен покрывать канон"
    youtube = TOOL_SPECS["live_youtube"]["schema"]["function"]["parameters"]
    assert youtube["properties"]["fullscreen"]["type"] == "boolean"
    assert youtube["required"] == ["query"]
    click = TOOL_SPECS["click_node"]["schema"]["function"]["parameters"]
    assert click["required"] == ["hwnd", "node"]
    fs = TOOL_SPECS["fs_trash_move"]["schema"]["function"]["parameters"]
    assert fs["properties"]["paths"]["type"] == "array"


def test_smart_brain_uses_dialogue_history(tmp_path, monkeypatch):
    """Вопрос уходит старшему мозгу с историей; ответ и реплика пишутся в память."""
    from mcp_hands.assistant import agent as agent_mod
    from mcp_hands.brain.llm import LLMConfig, LLMResponse

    seen_messages: list = []

    def fake_post(config, messages, timeout_s=60.0, tools=None):
        seen_messages.append([m["role"] for m in messages])
        return LLMResponse(text="Отвечаю на вопрос.", model="fake")

    monkeypatch.setattr(agent_mod, "_post_chat", fake_post)
    assistant = Assistant(config=LLMConfig(base_url="https://x", api_key="k", model="m"),
                          log_dir=tmp_path)
    assistant.dialogue.remember("user", "первый вопрос")
    assistant.dialogue.remember("assistant", "первый ответ", kind="smart")

    result = assistant.handle("а теперь второй")
    assert result["say"] == "Отвечаю на вопрос." and result["kind"] == "smart"
    roles = seen_messages[-1]
    assert roles[0] == "system" and roles[1:3] == ["user", "assistant"]  # история в промпте
    history = assistant.dialogue.history(6)
    assert history[-2]["content"] == "а теперь второй"
    assert history[-1]["content"] == "Отвечаю на вопрос."
