"""Мозг ассистента: исполнение локальных интентов и «старший мозг» с инструментами.

Порядок принятия решения (DESIGN: думать редко, делать часто):
1. локальный роутер (`router.route`) — мгновенно, без сети;
2. не распозналось — старший LLM (River/GLM) в режиме tool-calling по белому
   списку инструментов: модель выбирает вызовы, мы исполняем их В ПРОЦЕССЕ
   (те же функции, что у MCP-сервера), возвращаем результаты, получаем финальную
   фразу для голосового ответа.

Стоп-листы проекта действуют и здесь: набор инструментов ассистента не содержит
отправки/оплаты/публикации; удаление — только в корзину; окна пользователя не
трогаются без явного инструмента.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ..brain.llm import LLMConfig, LLMError, _post_chat, load_config
from .router import route

#: Каталог инструментов ассистента: строится АВТОМАТИЧЕСКИ из канона сервера
#: (TOOL_NAMES) — описание из докстринга, схема аргументов из аннотаций сигнатуры.
#: Ручной список был узким; расширять канон теперь достаточно в одном месте.
TOOL_SPECS: dict[str, dict] = {}

_TYPE_MAP = {str: "string", int: "integer", float: "number", bool: "boolean",
             list: "array", dict: "object"}
_STRING_TYPES = {"str": str, "int": int, "float": float, "bool": bool,
                 "list": list, "dict": dict}


def _schema_property(annotation) -> dict:
    """Тип параметра для LLM. Аннотации в модулях «отложены» (from __future__),
    поэтому строковые имена ('bool', 'list[str]') разрешаются вручную."""
    if isinstance(annotation, str):
        text_name = annotation.strip()
        if text_name.startswith("list"):
            return {"type": "array", "items": {"type": "string"}}
        if text_name.startswith("dict"):
            return {"type": "object"}
        annotation = _STRING_TYPES.get(text_name, annotation)
    if annotation in _TYPE_MAP:
        base = _TYPE_MAP[annotation]
        if base == "array":
            return {"type": "array", "items": {"type": "string"}}
        return {"type": base}
    return {"type": "string"}  # Optional/иные — строка, уточнение на стороне модели


def _build_registry() -> None:
    """Каталог = канон MCP-сервера; схемы выводятся из живых сигнатур функций."""
    if TOOL_SPECS:
        return
    import inspect

    from .. import mcp_server

    for name in sorted(mcp_server.TOOL_NAMES):
        function = getattr(mcp_server, name, None)
        if function is None or not callable(function):
            continue
        doc = (function.__doc__ or "").strip().splitlines()
        description = doc[0] if doc else name
        properties: dict = {}
        required: list[str] = []
        try:
            signature = inspect.signature(function)
        except (TypeError, ValueError):
            signature = None
        if signature is not None:
            for parameter in signature.parameters.values():
                if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
                    continue
                properties[parameter.name] = _schema_property(parameter.annotation)
                if parameter.default is inspect.Parameter.empty:
                    required.append(parameter.name)
        TOOL_SPECS[name] = {
            "function": function,
            "schema": {"type": "function", "function": {
                "name": name, "description": description,
                "parameters": {"type": "object", "properties": properties,
                               "required": required}}}}


def _tool_schemas() -> list[dict]:
    """Схемы для LLM: весь каталог ассистента (белый список = канон сервера)."""
    return [spec["schema"] for spec in TOOL_SPECS.values()]


#: Сколько знаков о результате инструмента уходит модели (см. tool_payload).
#: 2400 было мало: OCR окна отдаёт 200+ слов, и модель видела только ярлыки вкладок —
#: с этого начался провал «YouTube есть, а видимости нет» (лог владельца 19.10).
TOOL_PAYLOAD_LIMIT = 6000

#: Уровни усечения списков, если ответ всё ещё не влезает. Начинается с «отдать всё»:
#: раньше первым же шагом список ужимался до 12 записей и терял самое важное.
_SHRINK_STEPS = (None, 200, 60, 20, 8, 3, 1)

#: Слова-маркеры обрыва связи с браузером (CdpTransportError и его причины):
#: по ним ассистент отличает «живая сессия умерла» от «инструмент честно отказал».
TRANSPORT_MARKERS = ("обрыв связи", "связь с вкладкой", "ConnectionAborted",
                     "ConnectionReset", "не подключён")


def transport_lost(outcome: dict) -> bool:
    """Похоже ли, что причина отказа — потеря связи с браузером."""
    text = f"{outcome.get('reason', '')} {outcome.get('summary', '')}"
    return any(marker in text for marker in TRANSPORT_MARKERS)


def _shrink(value, keep: int):
    """Сжимает длинные списки в структуре: модели нужны первые записи, не все."""
    if isinstance(value, list):
        return [_shrink(item, keep) for item in value[:keep]]
    if isinstance(value, dict):
        return {key: _shrink(item, keep) for key, item in value.items()}
    return value


def tool_payload(outcome: dict, limit: int = TOOL_PAYLOAD_LIMIT) -> str:
    """Что модель видит о результате инструмента: вердикт И подробности (detail).

    Раньше уходили только вердикт и краткая сводка — и на вопрос «какие окна
    открыты» модель отвечала, что система вернула одно число: названия лежали
    в detail и до неё просто не доезжали. Усечение начинается с «отдать всё» и
    только при переполнении ужимает списки (шаги _SHRINK_STEPS) — редкие большие
    ответы теряют хвост, а не смысл.
    """
    detail = outcome.get("detail") or {}
    head = {"ok": outcome.get("ok"), "summary": outcome.get("summary", ""),
            "reason": outcome.get("reason", "")}
    text = json.dumps({**head, "detail": detail}, ensure_ascii=False, default=str)
    for keep in _SHRINK_STEPS:
        if len(text) <= limit:
            break
        trimmed = detail if keep is None else _shrink(detail, keep)
        text = json.dumps({**head, "detail": trimmed}, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + " (обрезано)"


SYSTEM_PROMPT = """Ты — Джарвис, голосовой ассистент владельца на Windows.
Отвечай КОРОТКО (одна-две фразы) по-русски: твой ответ будет произнесён голосом.

Как работать:
- это ВОПРОС или разговор — отвечай сам, без инструментов;
- это ДЕЙСТВИЕ на компьютере — выбери подходящий инструмент из доступных и вызови;
- это СЛОЖНАЯ задача — разложи на шаги и вызывай инструменты последовательно
  (за один раунд можно несколько), проверяй результат по фактам каждого вызова;
- в ответе инструмента всегда смотри поле detail: там сами факты (названия окон,
  вкладки, файлы, тексты). Отвечай по ним, а не по одному счётчику из summary;
- «НЕ НАЙДЕНО» и `found: false` — это ответ «элемента/текста нет», а не сбой:
  так и говори, без вторичных попыток угадать;
- на вопросы «видишь ли X на экране», «есть ли X», «открыто ли окно Y» отвечай
  фактом: `ocr_find` для текста на экране, `windows_list(query=...)` для окон;
  не проси «дай все слова кадра» — это лишние шаги и лишний шум;
- если в ответе есть `occlusion.covered` — окно перекрыто другим, в кадре чужое
  содержимое: скажи об этом честно, а не пересказывай чужой текст;
- если инструмент отказал — скажи причину и предложи следующий шаг; один и тот же
  вызов с теми же аргументами дважды не повторяй;
- не выдумывай инструменты и не обещай того, чего не проверил: если не вышло —
  честно скажи, что именно.
Помни историю разговора: «а теперь второй», «повтори» относятся к предыдущему."""


class Assistant:
    """Один сеанс Джарвиса: держит живой браузер и конфиг мозга."""

    def __init__(self, config: LLMConfig | None = None, log_dir: Path | None = None,
                 force_work: bool = False, keep_history: int = 8) -> None:
        from .dialogue import Dialogue

        _build_registry()
        self.config = config or load_config()
        self.log_dir = log_dir or Path(__file__).resolve().parents[2] / "logs" / "assistant"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.force_work = force_work  # приёмка: не трогать живую Opera владельца
        self.keep_history = keep_history
        self.last_url = ""  # страница, на которой владелец нас оставил
        self.dialogue = Dialogue(self.log_dir / "jarvis_memory.db")
        self.trace: list[dict] = []

    def _log(self, kind: str, **fields) -> None:
        entry = {"ts": round(time.time(), 3), "kind": kind, **fields}
        self.trace.append(entry)
        with (self.log_dir / "jarvis_trace.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # --- исполнение ---------------------------------------------------------

    def _call_tool(self, name: str, arguments: dict) -> dict:
        spec = TOOL_SPECS.get(name)
        if spec is None:
            return {"ok": False, "refused": True, "reason": f"инструмент {name} не разрешён"}
        try:
            result = spec["function"](**arguments)
        except Exception as exc:  # noqa: BLE001 — ошибка инструмента = вердикт
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
        return result.model_dump()

    def run_local(self, text: str) -> dict:
        """Локальный интент (мгновенный). Возвращает {"handled", "say", "detail"}."""
        intent = route(text)
        if intent is None:
            return {"handled": False}
        action, args = intent["action"], dict(intent["args"])
        answer: str | None = None  # фраза по факту (когда результат нельзя предсказать)
        if action == "navigate":
            outcome = self._ensure_browser(args["url"])
        elif action == "navigate_search":
            outcome = self._ensure_browser(args["url"])
            if outcome.get("ok"):
                outcome = self._live("live_search", {"query": args["query"]})
        elif action == "browser_open":
            outcome = self._ensure_browser()
        elif action == "search":
            outcome = self._ensure_browser()
            if outcome.get("ok"):
                outcome = self._live("live_search", args)
        elif action == "youtube":
            outcome = self._ensure_browser()
            if outcome.get("ok"):
                outcome = self._live("live_youtube", {**args, "fullscreen": True})
        elif action in ("video_pause", "video_play", "video_fullscreen", "video_exit"):
            outcome = self._live("live_video", {"action": action.split("_", 1)[1]})
        elif action == "screenshot":
            target = str(self.log_dir / time.strftime("shot_%H%M%S.png"))
            outcome = self._live("live_screenshot", {"path": target})
        elif action == "screen_look":
            # вопрос к глазам: ответ строится по факту кадра, а не по шаблону
            outcome = self._call_tool("ocr_find", {"text": args["text"]})
            detail = outcome.get("detail") or {}
            label = args.get("label", args["text"])
            if detail.get("found"):
                point = (detail.get("matches") or [{}])[0].get("rect") or [0, 0, 0, 0]
                answer = (f"Да, {label} видно на экране — "
                          f"в районе {point[0]}x{point[1]}.")
            else:
                answer = f"Нет, {label} на экране не вижу."
                blocked = (detail.get("occlusion") or {})
                if blocked.get("covered"):
                    who = (blocked.get("by") or [{}])[0].get("title", "")
                    answer += f" Часть экрана закрыта другим окном ({who[:40]})."
        elif action == "video_next":
            outcome = {"ok": False, "reason": "переключение видео пока не подключено"}
        else:
            outcome = {"ok": False, "reason": f"неизвестный интент {action}"}
        if answer is not None:
            say = answer
        elif outcome.get("ok"):
            say = intent["say"]
        else:
            say = "Не вышло: " + (outcome.get("reason") or "неизвестная причина")[:120]
        self.dialogue.remember("user", text)
        self.dialogue.remember("assistant", say, kind="local")
        self._log("local_intent", text=text, action=action, ok=outcome.get("ok"),
                  reason=outcome.get("reason", "")[:160],
                  steps=[{k: step.get(k) for k in ("step", "ok", "how", "found")}
                         for step in (outcome.get("steps") or [])])
        return {"handled": True, "say": say, "detail": {"action": action, **outcome}}

    def _live(self, tool: str, args: dict) -> dict:
        """Живой вызов с самолечением: обрыв связи → переподключение → повтор,
        а если и это не помогло — браузер поднимается заново и команда идёт снова.

        Владелец разговаривает с ассистентом минутами, и за это время живая
        сессия Opera может умереть: закрыли вкладку, уснул ноутбук, лёг процесс.
        Раньше сцена разваливалась на «WinError 10053»; теперь сначала
        перепривязываемся к вкладке (мягко), затем снимаем сессию и стартуем
        браузер заново (жёстко), и только после этого честно отказываем.
        """
        outcome = self._call_tool(tool, args)
        if outcome.get("ok") or not transport_lost(outcome):
            return outcome
        self._log("browser_reconnect", tool=tool, reason=str(outcome.get("reason"))[:160])
        revived = self._ensure_browser()
        if revived.get("ok"):
            retry = self._call_tool(tool, args)
            if retry.get("ok") or not transport_lost(retry):
                self._log("browser_reconnected", tool=tool)
                return retry
        self._log("browser_restart", tool=tool)
        fresh = self._ensure_browser(restart=True)
        if not fresh.get("ok"):
            return {"ok": False, "reason": "связь с браузером потеряна и поднять заново "
                                           "не удалось: " + str(fresh.get("reason"))[:120]}
        if self.last_url and tool != "live_navigate":
            # перезапуск открывает пустую вкладку: возвращаем владельца на ту страницу,
            # где его застал обрыв, иначе «пауза»/«скриншот» уйдут в about:blank
            restored = self._call_tool("live_navigate", {"url": self.last_url})
            self._log("browser_restored_page", url=self.last_url, ok=restored.get("ok"))
        return self._call_tool(tool, args)

    def _ensure_browser(self, url: str = "", restart: bool = False) -> dict:
        """Живой браузер: к ЖИВОЙ Opera владельца, если открыт порт; иначе свой.
        При `force_work` (приёмка/тесты) — всегда свой невидимый экземпляр, а в
        голосовом сценарии свой запускается ВИДИМЫМ: владелец должен видеть, что
        происходит на экране. `restart` сначала снимает мёртвую сессию."""
        if restart:
            self._call_tool("live_browser", {"action": "stop"})
        status = self._call_tool("live_browser", {"action": "status"})
        mode = (status.get("detail") or {}).get("mode") if status.get("ok") else ""
        if not mode:
            connected = {"ok": False}
            if not self.force_work:
                connected = self._call_tool("live_browser", {"action": "connect_user"})
            if not connected.get("ok"):
                connected = self._call_tool("live_browser",
                                            {"action": "start", "profile": "work",
                                             "headless": bool(self.force_work)})
            if not connected.get("ok"):
                return connected
        if url:
            outcome = self._live("live_navigate", {"url": url})
            if outcome.get("ok"):
                self.last_url = url  # куда вернуть владельца после перезапуска
            return outcome
        return {"ok": True}

    # --- старший мозг -------------------------------------------------------

    def run_smart(self, text: str, max_rounds: int = 8) -> dict:
        """Сложная фраза: GLM с инструментами, исполнение вызовов, финальная речь."""
        messages = ([{"role": "system", "content": SYSTEM_PROMPT}]
                    + self.dialogue.history(self.keep_history)
                    + [{"role": "user", "content": text}])
        used: list[dict] = []
        for _round in range(max_rounds):
            try:
                response = _post_chat(self.config, messages, tools=_tool_schemas())
            except LLMError as exc:
                self._log("smart", text=text, transport_error=str(exc))
                return {"say": "Связь с мозгом недоступна.", "detail": {"error": str(exc)}}
            if not response.tool_calls:
                self.dialogue.remember("user", text)
                self.dialogue.remember("assistant", response.text or "Готово.", kind="smart")
                self._log("smart", text=text, say=response.text[:160], used=used)
                return {"say": response.text or "Готово.", "detail": {"used": used}}
            messages.append({"role": "assistant", "content": response.text or "",
                             "tool_calls": [
                                 {"id": call["id"], "type": "function",
                                  "function": {"name": call["name"],
                                               "arguments": call["args"]}}
                                 for call in response.tool_calls]})
            for call in response.tool_calls:
                try:
                    arguments = json.loads(call["args"] or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                outcome = self._call_tool(call["name"], arguments)
                used.append({"tool": call["name"], "args": arguments,
                             "ok": outcome.get("ok"), "reason": outcome.get("reason", "")})
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": tool_payload(outcome)})
        self._log("smart", text=text, exhausted=True, used=used)
        say = self._final_say(messages)
        self.dialogue.remember("user", text)
        self.dialogue.remember("assistant", say, kind="smart")
        return {"say": say, "detail": {"used": used, "exhausted": True}}

    def _final_say(self, messages: list[dict]) -> str:
        """Шаги кончились: просим модель ответить БЕЗ инструментов по уже собранным
        фактам. Раньше владелец слышал только «Не удалось завершить за отведённые
        шаги» — теперь хотя бы отчёт о том, что получилось."""
        messages = messages + [{"role": "user", "content": (
            "Шаги закончились. Одной-двумя фразами скажи, что удалось и что нет — "
            "только по фактам выше, без новых вызовов инструментов.")}]
        try:
            final = _post_chat(self.config, messages)
        except LLMError:
            return "Не удалось завершить за отведённые шаги."
        return (final.text or "").strip() or "Не удалось завершить за отведённые шаги."

    def handle(self, text: str) -> dict:
        """Полный маршрут: локальный интент → старший мозг. Возвращает {"say", "kind"}."""
        local = self.run_local(text)
        if local.get("handled"):
            return {"say": local["say"], "kind": "local", "detail": local.get("detail", {})}
        smart = self.run_smart(text)
        return {"say": smart["say"], "kind": "smart", "detail": smart.get("detail", {})}
