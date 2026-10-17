"""Клиент «мозга» (этап 3, шаг 2): OpenAI-совместимый API на стандартном urllib.

Принципы (DESIGN §4.1 + план этапа 3):
1. LLM вызывается только для ПОСТРОЕНИЯ плана и перепланирования — не на каждый
   клик; исполнения занимаются локальные рефлексы ядра.
2. Режим ответа первичный — JSON-план по промпту: работает на любой модели,
   результат всегда прогоняется через strict-схему `brain.plan.parse_plan`.
3. Провайдер переключается локальным конфигом (`config.local.json`, вне git):
   `llm.{base_url, api_key, model, temperature, max_tokens, plan_attempts}`.
4. Ключ никогда не пишется в журнал, отчёты и исключения: в `LLMError` и логи
   уходит только код статуса и префикс ответа без заголовков.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from ..mcp_server import TOOL_NAMES
from .plan import Plan, parse_plan

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.local.json"


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.2
    max_tokens: int = 2048
    plan_attempts: int = 3


def load_config(path: Path = CONFIG_PATH) -> LLMConfig:
    """Читает локальный конфиг; отсутствие/пустота — внятная ошибка, не KeyError."""
    if not path.exists():
        raise RuntimeError(
            f"нет локального конфига {path}: создай его по шаблону "
            "(llm.base_url/api_key/model) — ключ в git не хранится и не коммитится")
    data = json.loads(path.read_text(encoding="utf-8"))
    llm = data.get("llm") or {}
    missing = [k for k in ("base_url", "api_key", "model") if not llm.get(k)]
    if missing:
        raise RuntimeError(f"в {path.name} не заполнены поля llm: {missing}")
    return LLMConfig(base_url=llm["base_url"].rstrip("/"), api_key=llm["api_key"],
                     model=llm["model"], temperature=float(llm.get("temperature", 0.2)),
                     max_tokens=int(llm.get("max_tokens", 2048)),
                     plan_attempts=int(llm.get("plan_attempts", 3)))


@dataclass
class LLMResponse:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    ms: float = 0.0
    raw: dict = field(default_factory=dict)


class LLMError(RuntimeError):
    """Отказ провайдера с безопасной формулировкой (без ключа и заголовков)."""


def _post_chat(config: LLMConfig, messages: list[dict], timeout_s: float = 60.0) -> LLMResponse:
    body = {"model": config.model, "messages": messages,
            "temperature": config.temperature, "max_tokens": config.max_tokens}
    request = urllib.request.Request(
        f"{config.base_url}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {config.api_key}"},
        method="POST")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        hint = exc.read().decode("utf-8", "replace")[:300] if exc.fp is not None else ""
        raise LLMError(f"провайдер вернул HTTP {exc.code}: {hint}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LLMError(f"сеть недоступна: {type(exc).__name__}: {exc}") from exc
    ms = (time.perf_counter() - started) * 1000.0
    try:
        text = payload["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"неожиданный формат ответа провайдера: {str(payload)[:300]}") from exc
    usage = payload.get("usage") or {}
    return LLMResponse(text=text, model=payload.get("model", config.model),
                       prompt_tokens=int(usage.get("prompt_tokens", 0)),
                       completion_tokens=int(usage.get("completion_tokens", 0)),
                       ms=round(ms, 1), raw=payload)


SYSTEM_PROMPT_TEMPLATE = """Ты — планировщик компьютерного агента MCP-Hands (Windows).
Ты строишь ПЛАН задачи из заранее заданных шагов; исполнение делают локальные
инструменты. Ты НЕ посылаешь клавиши и не придумываешь инструменты.

Доступные действия (строго этот список):
{actions}

Формат ответа: СТРОГО один JSON-объект без markdown, вида:
{{"task_id": "<id задачи>", "steps": [
  {{"action": "<имя из списка>", "args": {{...}},
    "expect": {{"kind": "<element_exists|text_contains|file_contains|window_open|process_gone|scenario_passed>", "params": {{...}}}}}}]}}

Правила:
- максимум 64 шага; каждый шаг обязан иметь expect (замкнутый цикл);
- домены "notepad_run", "word_pdf_run", "browser_links_run" закрывают свои
  задачи целиком и проверяются "scenario_passed" — не раскладывай их на мелочи;
- если своей задачей чувства не хватает (нет нужного инструмента), верни
  план с единственным шагом "health" и expect {{"kind": "process_gone",
  "params": {{"note": "missing_tools: <перечисление>"}}}} — так честно фиксируется запрет;
- никаких пояснений вне JSON."""

USER_PROMPT_TEMPLATE = """Задача {task_id}: {title}.
Требуемые инструменты задачи: {needs}.
Известные ограничения: {note}.
Построй план. Ответ — только JSON-объект."""


def build_plan_messages(task_id: str, title: str, needs: list[str], note: str = "") -> list[dict]:
    """Собирает [system, user] для запроса плана; действия — из канона сервера."""
    system = SYSTEM_PROMPT_TEMPLATE.format(actions=", ".join(sorted(TOOL_NAMES)))
    user = USER_PROMPT_TEMPLATE.format(task_id=task_id, title=title,
                                       needs=", ".join(needs) or "—", note=note or "—")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


@dataclass
class PlanRequestResult:
    plan: Plan | None
    attempts: list[dict] = field(default_factory=list)  # {attempt, ms, tokens, verdict}
    error: str = ""


def request_plan(config: LLMConfig, task_id: str, title: str, needs: list[str],
                 note: str = "") -> PlanRequestResult:
    """Запрашивает план у модели и валидирует схемой; невалидные попытки
    возвращаются модели на исправление (до plan_attempts), история — в attempts."""
    result = PlanRequestResult(plan=None)
    messages = build_plan_messages(task_id, title, needs, note)
    last_error = ""
    for attempt in range(1, max(1, config.plan_attempts) + 1):
        try:
            response = _post_chat(config, messages)
        except LLMError as exc:
            last_error = str(exc)
            result.attempts.append({"attempt": attempt, "transport_error": last_error})
            continue
        raw = {"attempt": attempt, "ms": response.ms,
               "prompt_tokens": response.prompt_tokens,
               "completion_tokens": response.completion_tokens}
        messages.append({"role": "assistant", "content": response.text})
        try:
            plan = parse_plan(response.text)
        except ValueError as exc:
            raw["verdict"] = f"схема отвергла: {exc}"
            messages.append({"role": "user", "content":
                             f"План не прошёл валидацию: {exc}. Верни исправленный JSON целиком."})
            result.attempts.append(raw)
            last_error = str(exc)
            continue
        raw["verdict"] = "ok"
        result.attempts.append(raw)
        result.plan = plan
        return result
    result.error = last_error or "модель не вернула валидный план за отведённые попытки"
    return result
