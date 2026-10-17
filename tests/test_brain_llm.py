"""Тесты клиента «мозга» (шаг 2): поведение по контракту, без реальной сети.

Живой вызов выделен в отдельный маркер `llm_live`: он требует config.local.json
(ключ владельца) и не должен срабатывать в обычных прогонах и песочницах.
"""

from __future__ import annotations

import json

import pytest

from mcp_hands.brain import llm as brain_llm
from mcp_hands.brain.llm import LLMConfig, LLMError, build_plan_messages, load_config, request_plan
from mcp_hands.mcp_server import TOOL_NAMES


def write_config(tmp_path, **overrides) -> "object":
    payload = {"llm": {"base_url": "https://api.example/v1", "api_key": "test-key",
                       "model": "test-model", **overrides}}
    path = tmp_path / "config.local.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_config_missing_and_partial_are_readable_errors(tmp_path):
    with pytest.raises(RuntimeError, match="нет локального конфига"):
        load_config(tmp_path / "absent.json")
    partial = tmp_path / "partial.json"
    partial.write_text(json.dumps({"llm": {"base_url": "x"}}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="api_key"):
        load_config(partial)


def test_system_prompt_carries_whitelist_and_rules():
    messages = build_plan_messages("b03", "Word → PDF", ["word_pdf_run"])
    system = messages[0]["content"]
    for tool in sorted(TOOL_NAMES):
        assert tool in system, f"планировщик должен видеть инструмент {tool}"
    assert "СТРОГО один JSON" in system and "expect" in system
    assert "посылаешь клавиши" in system and "не придумываешь инструменты" in system


def _fake_transport(responses, seen):
    """Подмена `_post_chat`: последовательность ответов (str) или ошибок (LLMError);
    `seen` копит копии сообщений каждого вызова — для проверки retry-диалога."""

    def probe(config: LLMConfig, messages: list[dict], timeout_s: float = 60.0):
        seen.append(list(messages))
        item = responses.pop(0)
        if isinstance(item, LLMError):
            raise item
        from mcp_hands.brain.llm import LLMResponse

        return LLMResponse(text=item, model=config.model, ms=1.0)

    return probe


def test_request_plan_retries_on_schema_violation(tmp_path, monkeypatch):
    config = load_config(write_config(tmp_path))
    bad = '{"task_id": "b03", "steps": [{"action": "delete_files_recursively", "expect": {"kind": "process_gone"}}]}'
    good = json.dumps({"task_id": "b03", "steps": [
        {"action": "word_pdf_run",
         "expect": {"kind": "scenario_passed", "params": {"task": "b03"}}}]})
    seen: list = []
    monkeypatch.setattr(brain_llm, "_post_chat", _fake_transport([bad, good], seen))
    result = request_plan(config, "b03", "Word → PDF", ["word_pdf_run"])
    assert result.plan is not None and result.plan.task_id == "b03"
    assert "delete_files_recursively" in result.attempts[0]["verdict"], result.attempts
    assert result.attempts[1]["verdict"] == "ok"
    # модель на ретрае получила собственный ответ и причину отказа валидации
    retry_messages = seen[-1]
    assert any(m.get("role") == "assistant" and "delete_files_recursively" in m.get("content", "")
               for m in retry_messages)
    assert any("не прошёл валидацию" in (m.get("content") or "") for m in retry_messages)


def test_request_plan_gives_up_after_budget(tmp_path, monkeypatch):
    config = load_config(write_config(tmp_path, plan_attempts=2))
    seen: list = []
    monkeypatch.setattr(brain_llm, "_post_chat",
                        _fake_transport([LLMError("сеть недоступна: таймаут")] * 2, seen))
    result = request_plan(config, "b03", "Word → PDF", ["word_pdf_run"])
    assert result.plan is None and "сеть" in result.error
    assert len(result.attempts) == 2, "бюджет попыток из конфига соблюдён"


def test_config_roundtrip_defaults(tmp_path):
    """Круговое чтение конфига: поля на месте, дефолты подставлены, ключ доступен
    только через объект конфига (не через исключения и логи)."""
    config = load_config(write_config(tmp_path, plan_attempts=2))
    assert config.base_url == "https://api.example/v1"
    assert config.api_key == "test-key"
    assert config.model == "test-model"
    assert config.temperature == 0.2 and config.max_tokens == 2048
    assert config.plan_attempts == 2


@pytest.mark.llm_live
@pytest.mark.skipif(not brain_llm.CONFIG_PATH.exists(), reason="нет config.local.json (ключ владельца)")
def test_live_plan_from_river():
    config = load_config()
    result = request_plan(config, "b03", "Word → PDF: сохранение по шаблону имени",
                          ["word_pdf_run"])
    assert result.plan is not None, result.error or result.attempts
    assert result.plan.steps[-1].expect.kind == "scenario_passed"
