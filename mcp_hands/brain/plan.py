"""Схема плана (этап 3): strict JSON, который выдаёт «мозг» и исполняет runner.

Правила безопасности зашиты в схему:
1. `action` ∈ канонический набор инструментов сервера (`TOOL_NAMES`) — стоп-лист
   наследуется архитектурно: не existe инструмента → план не валиден.
2. `extra="forbid"` — LLM не может пронести неизвестные поля (инъекцию
   параметров исполнения).
3. Каждый шаг ОБЯЗАН объявить ожидаемую постусловие `expect` — closed-loop
   (DESIGN §4.1): не наступило → локальный повтор → перепланирование.
   Сценарные домены (`*_run`) проверяют постусловия внутри и валидны с
   `expect.kind = "scenario_passed"`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..mcp_server import TOOL_NAMES

EXPECT_KINDS = {
    "element_exists", "text_contains", "file_contains",
    "window_open", "process_gone", "scenario_passed"}


class Expect(BaseModel):
    """Ожидаемая постусловие шага. Параметры зависят от kind (свободный словарь:
   _runner проверяет поддержку kind и набор полей перед исполнением)."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    params: dict = Field(default_factory=dict)

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, value: str) -> str:
        if value not in EXPECT_KINDS:
            raise ValueError(f"неизвестная постусловие {value!r}; допустимо: {sorted(EXPECT_KINDS)}")
        return value


class Step(BaseModel):
    """Один шаг плана: действие над ядром + ожидаемый факт о мире после него."""

    model_config = ConfigDict(extra="forbid")

    action: str
    args: dict = Field(default_factory=dict)
    expect: Expect
    note: str = ""

    @field_validator("action")
    @classmethod
    def _action_in_whitelist(cls, value: str) -> str:
        if value not in TOOL_NAMES:
            raise ValueError(
                f"действие {value!r} не входит в набор mcp-hands: {sorted(TOOL_NAMES)}")
        return value


class Plan(BaseModel):
    """План задачи: идентификатор задачи бенчмарка и последовательность шагов."""

    model_config = ConfigDict(extra="forbid")

    plan_version: str = "3.0"
    task_id: str
    steps: list[Step] = Field(min_length=1, max_length=64)

    @field_validator("steps")
    @classmethod
    def _steps_capped(cls, value: list[Step]) -> list[Step]:
        if len(value) > 64:
            raise ValueError("план слишком длинный: максимум 64 шага")
        return value


def parse_plan(raw: str | dict) -> Plan:
    """Единственная точка входа для выходов LLM: строка или словарь → Plan.
    Любое несоответствие — ValueError с человеческой причиной (для перепланирования)."""
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"план не является JSON: {exc}") from exc
    return Plan.model_validate(raw)
