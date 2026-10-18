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

#: Белый список инструментов ассистента: имя → (функция, описание, схема аргументов)
TOOL_SPECS: dict[str, dict] = {}


def _register(name: str, description: str, properties: dict, required: list[str]):
    from .. import mcp_server

    TOOL_SPECS[name] = {
        "function": getattr(mcp_server, name),
        "schema": {"type": "function", "function": {
            "name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": required}}},
    }


def _build_registry() -> None:
    if TOOL_SPECS:
        return
    _register("live_browser", "Живой браузер Opera: start (свой профиль, "
              "headless) / connect_user (живая Opera владельца) / stop / status.",
              {"action": {"type": "string"}, "profile": {"type": "string"},
               "headless": {"type": "boolean"}}, ["action"])
    _register("live_navigate", "Перейти по адресу в живом браузере.",
              {"url": {"type": "string"}}, ["url"])
    _register("live_page", "Что сейчас на странице: адрес, заголовок, поля поиска.",
              {}, [])
    _register("live_search", "Напечатать запрос в поиске текущей страницы.",
              {"query": {"type": "string"}}, ["query"])
    _register("live_click_text", "Клик по элементу страницы с заданным текстом.",
              {"text": {"type": "string"}}, ["text"])
    _register("live_video", "Видео на странице: status/play/pause/fullscreen/exit.",
              {"action": {"type": "string"}}, ["action"])
    _register("live_screenshot", "Скриншот страницы (PNG-файл).",
              {"path": {"type": "string"}}, [])
    _register("live_youtube", "Найти видео по запросу на YouTube и включить "
              "(по флагу — на весь экран).",
              {"query": {"type": "string"}, "fullscreen": {"type": "boolean"}},
              ["query"])
    _register("windows_list", "Список открытых окон (hwnd, процесс, заголовок).",
              {}, [])
    _register("element_find", "Найти элемент в окне по имени/типу (UIA).",
              {"hwnd": {"type": "integer"}, "name": {"type": "string"},
               "control_type": {"type": "string"}}, ["hwnd"])
    _register("element_text", "Прочитать текст окна через TextPattern.",
              {"hwnd": {"type": "integer"}}, ["hwnd"])


def _tool_schemas() -> list[dict]:
    return [spec["schema"] for spec in TOOL_SPECS.values()]


SYSTEM_PROMPT = """Ты — Джарвис, голосовой ассистент владельца на Windows.
Отвечай КОРОТКО (одна-две фразы) по-русски: твой ответ будет произнесён голосом.
Управляй компьютером ТОЛЬКО через предоставленные инструменты; никогда не выдумывай их.
Если нужен живой браузер — сначала live_browser(action="start"), затем навигация.
Для «найди видео X и включи» используй live_youtube.
Факты проверяй по результатам инструментов и говори честно, если не получилось."""


class Assistant:
    """Один сеанс Джарвиса: держит живой браузер и конфиг мозга."""

    def __init__(self, config: LLMConfig | None = None, log_dir: Path | None = None,
                 force_work: bool = False) -> None:
        _build_registry()
        self.config = config or load_config()
        self.log_dir = log_dir or Path(__file__).resolve().parents[2] / "logs" / "assistant"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.force_work = force_work  # приёмка: не трогать живую Opera владельца
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
        if action == "navigate":
            outcome = self._ensure_browser(args["url"])
        elif action == "navigate_search":
            outcome = self._ensure_browser(args["url"])
            if outcome.get("ok"):
                outcome = self._call_tool("live_search", {"query": args["query"]})
        elif action == "search":
            outcome = self._ensure_browser()
            if outcome.get("ok"):
                outcome = self._call_tool("live_search", args)
        elif action == "youtube":
            outcome = self._ensure_browser()
            if outcome.get("ok"):
                outcome = self._call_tool("live_youtube", {**args, "fullscreen": True})
        elif action in ("video_pause", "video_play", "video_fullscreen", "video_exit"):
            outcome = self._call_tool("live_video", {"action": action.split("_", 1)[1]})
        elif action == "screenshot":
            target = str(self.log_dir / time.strftime("shot_%H%M%S.png"))
            outcome = self._call_tool("live_screenshot", {"path": target})
        elif action == "video_next":
            outcome = {"ok": False, "reason": "переключение видео пока не подключено"}
        else:
            outcome = {"ok": False, "reason": f"неизвестный интент {action}"}
        say = intent["say"] if outcome.get("ok") else (
            "Не вышло: " + (outcome.get("reason") or "неизвестная причина")[:120])
        self._log("local_intent", text=text, action=action, ok=outcome.get("ok"),
                  reason=outcome.get("reason", "")[:160],
                  steps=[{k: step.get(k) for k in ("step", "ok", "how", "found")}
                         for step in (outcome.get("steps") or [])])
        return {"handled": True, "say": say, "detail": {"action": action, **outcome}}

    def _ensure_browser(self, url: str = "") -> dict:
        """Живой браузер: к ЖИВОЙ Opera владельца, если открыт порт; иначе свой.
        При `force_work` (приёмка/тесты) — всегда свой невидимый экземпляр."""
        status = self._call_tool("live_browser", {"action": "status"})
        mode = (status.get("detail") or {}).get("mode") if status.get("ok") else ""
        if not mode:
            connected = {"ok": False}
            if not self.force_work:
                connected = self._call_tool("live_browser", {"action": "connect_user"})
            if not connected.get("ok"):
                connected = self._call_tool("live_browser",
                                            {"action": "start", "profile": "work",
                                             "headless": True})
            if not connected.get("ok"):
                return connected
        if url:
            return self._call_tool("live_navigate", {"url": url})
        return {"ok": True}

    # --- старший мозг -------------------------------------------------------

    def run_smart(self, text: str, max_rounds: int = 5) -> dict:
        """Сложная фраза: GLM с инструментами, исполнение вызовов, финальная речь."""
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": text}]
        used: list[dict] = []
        for _round in range(max_rounds):
            try:
                response = _post_chat(self.config, messages, tools=_tool_schemas())
            except LLMError as exc:
                self._log("smart", text=text, transport_error=str(exc))
                return {"say": "Связь с мозгом недоступна.", "detail": {"error": str(exc)}}
            if not response.tool_calls:
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
                                 "content": json.dumps(
                                     {"ok": outcome.get("ok"),
                                      "summary": outcome.get("summary", ""),
                                      "reason": outcome.get("reason", "")},
                                     ensure_ascii=False)})
        self._log("smart", text=text, exhausted=True, used=used)
        return {"say": "Не удалось завершить за отведённые шаги.", "detail": {"used": used}}

    def handle(self, text: str) -> dict:
        """Полный маршрут: локальный интент → старший мозг. Возвращает {"say", "kind"}."""
        local = self.run_local(text)
        if local.get("handled"):
            return {"say": local["say"], "kind": "local", "detail": local.get("detail", {})}
        smart = self.run_smart(text)
        return {"say": smart["say"], "kind": "smart", "detail": smart.get("detail", {})}
