"""MCP-сервер: экспонирование ядра `mcp_hands` по протоколу Model Context Protocol.

Этап 2. Правила дизайна (вытекают из аудитов этапа 1 и финального аудита v10):

1. **Единый контракт результата** — `ToolResult` (`ok`, `refused`, `reason`,
   `summary`, `detail`) у всех инструментов: клиент читает одно и то же лицо,
   а не догадывается по стеку. Отказ стража — это нормальный результат
   (`refused=True`), а не протокол-ошибка и не исключение сервера.
2. **Стражи ядра не обходятся**: у действий клавиатуры параметр `expect_hwnd`
   обязателен, при его отсутствии возвращается `refused` без отправки событий
   (тот же контракт, что в `Actuator.guard_keyboard`).
3. **Стоп-лист — на уровне набора**: в сервере НЕТ инструментов отправки,
   оплаты, публикации и удаления. Денег/интернета/необратимых действий ядро
   не экспонирует.
4. **Журнал операций** (`logs/mcp_calls.log`, JSONL): инструмент, длительность,
   вердикт; тексты введённого содержимого НЕ пишутся — только длина.
5. Импорт ядра ленивый: сервер поднимается быстро, тяжёлые зависимости
   (comtypes, winsdk, dxcam) тянутся первым вызовом.

Запуск: `python -m mcp_hands.mcp_server` (транспорт stdio).
"""

from __future__ import annotations

import json
import logging
import platform
import sys
import time
from pathlib import Path

from pydantic import BaseModel

from mcp.server.mcpserver import MCPServer

LOG_PATH = Path(__file__).resolve().parents[1] / "logs" / "mcp_calls.log"

# Канонический набор инструментов: единый источник истины для сквозных тестов,
# реестра задач бенчмарка и текста системного промпта планировщика. Расширение
# набора — сознательная операция: стоп-лист безопасности работает «отсутствием».
TOOL_NAMES = frozenset({
    "windows_list", "element_find", "element_text", "content_map", "ocr_window",
    "window_activate", "click_node", "type_text", "hotkey",
    "notepad_run", "word_pdf_run", "browser_links_run", "health"})

server = MCPServer(
    name="mcp-hands",
    title="MCP-Hands",
    description=("Руки и глаза: UIA-восприятие, ввод под стражем, доменные сценарии "
                 "Windows (Word→PDF, браузер CDP, Блокнот). Windows-only."),
    instructions=(
        "Восприятие через UIA дешевле пикселей; действуй API-first. Любой клавиатурный "
        "инструмент требует expect_hwnd (дескриптор активного окна) — без него "
        "откажется. Инструменты отправки/оплаты/публикации отсутствуют by design."),
    version="0.1.0",
)

# --- ленивые одиночки ядра -----------------------------------------------------

_space = _actuator = _uia = _ocr_engine = _grabber = None


def _coordinate_space():
    global _space
    if _space is None:
        from .geometry import CoordinateSpace, enable_dpi_awareness

        enable_dpi_awareness()
        _space = CoordinateSpace()
    return _space


def _ensure_com() -> None:
    """COM-инициализация текущего потока. comtypes делает её только при импорте:
    в сервере (in-memory тесты, вызовы из задач эвент-лупа другими потоками) без
    явного вызова UIA падает с CO_E_NOTINITIALIZED (WinError -2147221008)."""
    import comtypes

    comtypes.CoInitialize()


def _act():
    global _actuator
    if _actuator is None:
        from .input import Actuator

        _actuator = Actuator(_coordinate_space())
    return _actuator


def _automation():
    global _uia
    if _uia is None:
        _ensure_com()
        from .uia import UiaClient

        _uia = UiaClient()
    return _uia


def _ocr():
    global _ocr_engine
    if _ocr_engine is None:
        from .ocr import WindowsOcr

        _ocr_engine = WindowsOcr()
    return _ocr_engine


def _screen():
    global _grabber
    if _grabber is None:
        from .screen import ScreenGrabber

        _grabber = ScreenGrabber()
    return _grabber


# --- контракт результата и журнал -----------------------------------------------


class ToolResult(BaseModel):
    """Единое лицо всех инструментов: вердикт, причина, детали."""

    ok: bool
    summary: str = ""
    refused: bool = False
    reason: str = ""
    detail: dict = {}


def _journal(tool: str, started: float, result: ToolResult, note_args: dict) -> None:
    """Одна строка JSONL на вызов. Тексты введённого не пишем — только длины."""
    entry = {"ts": round(time.time(), 3), "tool": tool,
             "ms": round((time.perf_counter() - started) * 1000, 1),
             "ok": result.ok, "refused": result.refused,
             "args": {k: (f"<сложено {len(v)} знаков>" if k == "text" and isinstance(v, str)
                          else v) for k, v in note_args.items()}}
    if not result.ok:
        entry["reason"] = result.reason
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:  # noqa: BLE001 — журнал не должен ломать инструмент
        logging.getLogger(__name__).warning("журнал вызовов недоступен: %s", LOG_PATH)


def _fail(tool: str, started: float, reason: str, refused: bool = False,
          **note_args) -> ToolResult:
    result = ToolResult(ok=False, refused=refused, reason=reason, summary=reason)
    _journal(tool, started, result, note_args)
    return result


# --- восприятие ------------------------------------------------------------------


@server.tool(structured_output=True)
def windows_list() -> ToolResult:
    """Список верхнеуровневых окон (hwnd, pid, процесс, заголовок, прямоугольник)."""
    started = time.perf_counter()
    try:
        from .uia import enum_windows

        rows = enum_windows()
    except Exception as exc:  # noqa: BLE001
        return _fail("windows_list", started, f"{type(exc).__name__}: {exc}")
    result = ToolResult(ok=True, summary=f"окон: {len(rows)}", detail={"windows": rows})
    _journal("windows_list", started, result, {})
    return result


@server.tool(structured_output=True)
def element_find(hwnd: int, name: str = "", control_type: str = "",
                 automation_id: str = "", scope: str = "shallow",
                 depth: int = 3) -> ToolResult:
    """Поиск одного элемента в окне по имени/типу/automation_id (UIA, адресный запрос)."""
    started = time.perf_counter()
    if not (name or control_type or automation_id):
        return _fail("element_find", started, "нужно хотя бы одно условие поиска", True)
    try:
        _ensure_com()
        client = _automation()
        node = client.find(client.window(hwnd), name=name, control_type=control_type,
                           automation_id=automation_id, scope=scope, depth=depth)
    except Exception as exc:  # noqa: BLE001
        return _fail("element_find", started, f"{type(exc).__name__}: {exc}",
                     hwnd=hwnd, name=name)
    if not node:
        result = ToolResult(ok=True, refused=False, summary="элемент не найден",
                            detail={"found": False})
        _journal("element_find", started, result, {"hwnd": hwnd, "name": name})
        return result
    found = {"ok": True, "summary": f"найдено: {node.get('name', '')!r}",
             "refused": False, "reason": "",
             "detail": {"found": True, "node": node}}
    result = ToolResult(**found)
    _journal("element_find", started, result, {"hwnd": hwnd, "name": name})
    return result


@server.tool(structured_output=True)
def element_text(hwnd: int, max_length: int = 4000) -> ToolResult:
    """Текст окна через TextPattern (документ, страница; пусто, если паттерна нет)."""
    started = time.perf_counter()
    try:
        _ensure_com()
        client = _automation()
        root, _info = client.content_root(hwnd, jump=True)
        from .uia import element_text

        text = element_text(root)
    except Exception as exc:  # noqa: BLE001
        return _fail("element_text", started, f"{type(exc).__name__}: {exc}", hwnd=hwnd)
    cut = text[:max_length]
    result = ToolResult(ok=True, summary=f"{len(text)} знаков",
                        detail={"text": cut, "truncated": len(text) > len(cut)})
    _journal("element_text", started, result, {"hwnd": hwnd})
    return result


@server.tool(structured_output=True)
def content_map(hwnd: int, depth: int = 3) -> ToolResult:
    """Карта дерева окна (UIA): тип, имя,automation_id, прямоугольник; до depth уровней."""
    started = time.perf_counter()
    try:
        _ensure_com()
        client = _automation()
        walked = client.walk(client.window(hwnd), depth=depth, scope="deep")
        nodes = walked.get("nodes", [])[:600]
    except Exception as exc:  # noqa: BLE001
        return _fail("content_map", started, f"{type(exc).__name__}: {exc}",
                     hwnd=hwnd, depth=depth)
    result = ToolResult(ok=True, summary=f"узлов: {len(nodes)} (обрезано, лимит 600)",
                        detail={"count": len(nodes), "nodes": nodes})
    _journal("content_map", started, result, {"hwnd": hwnd, "depth": depth})
    return result


@server.tool(structured_output=True)
def ocr_window(hwnd: int) -> ToolResult:
    """OCR окна: кроп видимой части + встроенный OCR Windows; слова в экранных координатах."""
    started = time.perf_counter()
    from .screen import ScreenCaptureError

    try:
        frame = _screen().crop_window(_coordinate_space(), hwnd,
                                      _screen().grab(timeout_s=2.0))
        words = _ocr().recognize(frame.image, offset=(frame.origin[0], frame.origin[1]))
    except ScreenCaptureError as exc:
        return _fail("ocr_window", started, f"захват экрана отказал: {exc}",
                     refused=True, hwnd=hwnd)
    except Exception as exc:  # noqa: BLE001
        return _fail("ocr_window", started, f"{type(exc).__name__}: {exc}", hwnd=hwnd)
    rows = [w.to_dict() for w in words]
    result = ToolResult(ok=True, summary=f"слов: {len(rows)}",
                        detail={"words": rows, "ms": round(_ocr().last_ms or 0.0, 1)})
    _journal("ocr_window", started, result, {"hwnd": hwnd})
    return result


# --- действия ---------------------------------------------------------------------


@server.tool(structured_output=True)
def window_activate(hwnd: int) -> ToolResult:
    """Активация окна (<1.5 с) — SetForegroundWindow + страховка AttachThreadInput."""
    started = time.perf_counter()
    try:
        outcome = _act().activate(hwnd)
    except Exception as exc:  # noqa: BLE001
        return _fail("window_activate", started, f"{type(exc).__name__}: {exc}", hwnd=hwnd)
    result = ToolResult(ok=bool(outcome.get("ok")),
                        reason=outcome.get("reason", ""), summary=outcome.get("how", ""),
                        detail=outcome)
    _journal("window_activate", started, result, {"hwnd": hwnd})
    return result


@server.tool(structured_output=True)
def click_node(hwnd: int, node: dict, activate: bool = True,
               dry_run: bool = False) -> ToolResult:
    """Клик в элемент по его UIA-карточке из `element_find`: PID берётся из узла,
    перед кликом проверяются рабочий стол и права (UIPI) — слепых кликов нет.

    `dry_run=True` прогоняет ту же цепочку проверок (узел, права, стол, живое окно)
    и возвращает точку клика, НЕ отправляя ввод и НЕ активируя окно."""
    started = time.perf_counter()
    if not node.get("name") and not node.get("automation_id"):
        return _fail("click_node", started, "узел без имени и automation_id: висящий клик",
                     True)
    try:
        if dry_run:
            checks = _dry_run_click_checks(hwnd, node)
            result = ToolResult(ok=checks["ok"], refused=False,
                                reason="" if checks["ok"] else checks.get("reason", ""),
                                summary="сухой прогон: клик был бы отправлен" if checks["ok"]
                                        else "сухой прогон: клик был бы отклонён",
                                detail=checks)
            _journal("click_node", started, result, {"dry_run": True})
            return result
        outcome = _act().click_node(node, pid=int(node.get("process_id") or 0),
                                    activate=activate, window_hwnd=hwnd)
    except Exception as exc:  # noqa: BLE001
        return _fail("click_node", started, f"{type(exc).__name__}: {exc}")
    delivered = bool(outcome.get("ok"))
    result = ToolResult(ok=delivered, refused=not delivered,
                        reason=outcome.get("reason", ""),
                        summary=("клик доставлен и подтверждён курсором" if delivered
                                 else "клик не доставлен — отказа ядра"),
                        detail=outcome)
    _journal("click_node", started, result, {})
    return result


@server.tool(structured_output=True)
def type_text(hwnd: int, text: str, verify_document: bool = False,
              attempts: int = 3, dry_run: bool = False) -> ToolResult:
    """Ввод текста в проверенно активное окно (страж `expect_hwnd` обязателен).

    `verify_document=True` — после ввода перечитать документ окна (TextPattern)
    и подтвердить совпадение; режим для полей-документов (Блокнот и подобные),
    где весь текст поля и есть введённое.
    `dry_run=True` проверяет готовность (страж активного окна, подсчёт code units
    UTF-16 для контрольного круга) и НЕ отправляет ни одного события.
    """
    started = time.perf_counter()
    if not text:
        return _fail("type_text", started, "пустой текст", True)
    try:
        if dry_run:
            guard = _act().guard_keyboard(hwnd)
            units = sum(len(char.encode("utf-16-le")) // 2 for char in text)
            detail = {"dry_run": True, "chars": len(text), "code_units": units,
                      "enters": text.count("\n"), "foreground": _act().foreground(),
                      "expect_hwnd": hwnd, "would_succeed": bool(guard.get("ok")),
                      "guard_reason": guard.get("reason", "")}
            result = ToolResult(ok=bool(guard.get("ok")), refused=not guard.get("ok"),
                                reason=guard.get("reason", ""),
                                summary=("сухой прогон: ввод был бы отправлен"
                                         if guard.get("ok")
                                         else "сухой прогон: ввод был бы отменён стражем"),
                                detail=detail)
            _journal("type_text", started, result, {"dry_run": True})
            return result
        if verify_document:
            _ensure_com()
            client = _automation()

            def read_back() -> str:
                root, _info = client.content_root(hwnd, jump=True)
                from .uia import element_text

                return element_text(root)

            outcome = _act().type_text_verified(text, read_back, attempts=attempts,
                                                expect_hwnd=hwnd)
        else:
            outcome = _act().type_text(text, expect_hwnd=hwnd)
    except Exception as exc:  # noqa: BLE001
        return _fail("type_text", started, f"{type(exc).__name__}: {exc}", hwnd=hwnd)
    guard_refused = not outcome.get("ok") and "окно" in (outcome.get("reason") or "")
    result = ToolResult(ok=bool(outcome.get("ok")), refused=guard_refused,
                        reason=outcome.get("reason", ""),
                        summary=(f"введено {outcome.get('chars', 0)} знаков"
                                 if outcome.get("ok") else "ввод отменён"),
                        detail=outcome)
    _journal("type_text", started, result, {"hwnd": hwnd, "text": text})
    return result


VK_BY_NAME = {
    "CTRL": 0x11, "CONTROL": 0x11, "SHIFT": 0x10, "ALT": 0x12,
    "TAB": 0x09, "ENTER": 0x0D, "RETURN": 0x0D, "ESC": 0x1B, "ESCAPE": 0x1B,
    "DELETE": 0x2E, "DEL": 0x2E, "BACK": 0x08, "SPACE": 0x20, "HOME": 0x24,
    "END": 0x23, "PAGEDOWN": 0x22, "PAGEUP": 0x21,
    "A": 0x41, "C": 0x43, "F": 0x46, "N": 0x4E, "O": 0x4F, "P": 0x50,
    "R": 0x52, "S": 0x53, "T": 0x54, "V": 0x56, "W": 0x57, "X": 0x58, "Z": 0x5A,
}


@server.tool(structured_output=True)
def hotkey(hwnd: int, keys: list[str], dry_run: bool = False) -> ToolResult:
    """Сочетание клавиш в проверенно активное окно, например ["CTRL", "S"].

    Белый список клавиш (нет): системных (WIN), F1–F12, стрелок — по соображениям
    безопасности сначала доказываем полезность с узкого набора.
    `dry_run=True` проверяет белый список и страж активного окна, НЕ отправляя
    ни одного события.
    """
    started = time.perf_counter()
    unknown = [key for key in keys if VK_BY_NAME.get(key.upper()) is None]
    if not keys or unknown:
        return _fail("hotkey", started,
                     f"неизвестные или запрещённые клавиши: {unknown}; допустимо: CTRL/SHIFT/ALT "
                     f"+ буква/TAB/ENTER/ESC/DEL/BACK/SPACE/HOME/END/PAGEUP/PAGEDOWN", True)
    try:
        if dry_run:
            guard = _act().guard_keyboard(hwnd)
            detail = {"dry_run": True,
                      "would_send": [VK_BY_NAME[key.upper()] for key in keys],
                      "foreground": _act().foreground(), "expect_hwnd": hwnd,
                      "would_succeed": bool(guard.get("ok")),
                      "guard_reason": guard.get("reason", "")}
            result = ToolResult(ok=bool(guard.get("ok")), refused=not guard.get("ok"),
                                reason=guard.get("reason", ""),
                                summary=("сухой прогон: нажатие было бы отправлено"
                                         if guard.get("ok")
                                         else "сухой прогон: нажатие было бы отменено"),
                                detail=detail)
            _journal("hotkey", started, result, {"keys": keys, "dry_run": True})
            return result
        outcome = _act().hotkey(*(VK_BY_NAME[key.upper()] for key in keys), expect_hwnd=hwnd)
    except Exception as exc:  # noqa: BLE001
        return _fail("hotkey", started, f"{type(exc).__name__}: {exc}", keys=keys)
    refused = not outcome.get("ok")
    result = ToolResult(ok=bool(outcome.get("ok")), refused=refused,
                        reason=outcome.get("reason", ""),
                        summary=("нажатие отправлено" if outcome.get("ok")
                                 else "нажатие отменено стражем"),
                        detail=outcome)
    _journal("hotkey", started, result, {"keys": keys})
    return result


# --- доменные сценарии ------------------------------------------------------------


def _dry_run_click_checks(hwnd: int, node: dict) -> dict:
    """Проверки dry_run для клика: та же лестница стражей ядра, ни одного ввода."""
    checks: dict = {"dry_run": True, "ok": False}
    rect = node.get("rect")
    if not rect:
        checks["reason"] = "у элемента нет прямоугольника"
        return checks
    checks["center"] = node.get("center") or [(rect[0] + rect[2]) // 2,
                                              (rect[1] + rect[3]) // 2]
    pid = int(node.get("process_id") or 0)
    checks["pid"] = pid
    act = _act()
    if pid:
        rights = act.can_control(pid)
        checks["rights"] = rights
        if rights.get("blocked_by_uipi"):
            checks["reason"] = "целевое окно с более высокими правами: ввод не дойдёт (UIPI)"
            return checks
    ours, theirs = act.own_desktop(), act.window_desktop(hwnd)
    checks["desktop"] = {"ours": ours, "target": theirs}
    if ours and theirs and not ours.startswith("<") and not theirs.startswith("<") \
            and ours != theirs:
        checks["reason"] = f"окно цели на столе {theirs!r}, ввод идёт на {ours!r}"
        return checks
    if not act.window_alive(hwnd):
        checks["reason"] = "окно цели больше не существует"
        return checks
    checks["ok"] = True
    return checks


def _run_domain(tool: str, module_name: str) -> ToolResult:
    started = time.perf_counter()
    # Сценарии касаются COM (UiaClient и Office) прямо в setup, до первого шага:
    # без инициализации в потоке инструмента первый же COM-вызов падает с
    # CO_E_NOTINITIALIZED (поймано живым прогоном через хост, аудит v13).
    _ensure_com()
    try:
        from importlib import import_module

        from .scenarios.runner import run_scenario

        module = import_module(f".scenarios.{module_name}", __package__)
        # Имя прогона явное: __name__ у всех сценариев «scenario», и без него
        # отчёты трёх доменов сваливаются в общий каталог.
        summary = run_scenario(module.scenario, repeats=1, name=module_name,
                               verbose=False)
    except Exception as exc:  # noqa: BLE001
        return _fail(tool, started, f"{type(exc).__name__}: {exc}")
    detail = {
        "passed": summary.get("passed"), "attempts": summary.get("attempts"),
        "total_ms": summary.get("results", [{}])[0].get("total_ms"),
        "failed_steps": summary.get("results", [{}])[0].get("failed", []),
        "report": summary.get("results", [{}])[0].get("report")}
    result = ToolResult(ok=bool(summary.get("passed") == summary.get("attempts")
                                and summary.get("passed")),
                        summary=f"сценарий {summary.get('passed')}/{summary.get('attempts')}",
                        reason=("шаги не прошли: " + ", ".join(detail["failed_steps"]))
                                if detail["failed_steps"] else "",
                        detail=detail)
    _journal(tool, started, result, {})
    return result


@server.tool(structured_output=True)
def notepad_run() -> ToolResult:
    """Сценарий Блокнота целиком: открыть файл → ввести текст → сохранить → закрыть
    вкладку по протоколу (проверка заголовка, страж, постусловие)."""
    return _run_domain("notepad_run", "notepad")


@server.tool(structured_output=True)
def word_pdf_run() -> ToolResult:
    """Сценарий Word → PDF целиком: документ по шаблону → замена плейсхолдеров →
    экспорт PDF → сверка текста PDF. Ввод не использует (COM)."""
    return _run_domain("word_pdf_run", "word_pdf")


@server.tool(structured_output=True)
def browser_links_run() -> ToolResult:
    """Сценарий браузера целиком: headless Edge + CDP → собрать ссылки → проверить файл."""
    return _run_domain("browser_links_run", "browser_links")


# --- служебные ---------------------------------------------------------------------


@server.tool(structured_output=True)
def health() -> ToolResult:
    """Состояние рук: DPI, рабочий стол, python, версия набора; без побочных эффектов."""
    started = time.perf_counter()
    detail = {"python": platform.python_version(), "machine": platform.machine()}
    try:
        detail["dpi_aware"] = _coordinate_space() is not None
        detail["desktop"] = _act().own_desktop()
        ok = True
        summary = "руки доступны"
    except Exception as exc:  # noqa: BLE001
        detail["error"] = f"{type(exc).__name__}: {exc}"
        ok, summary = False, "руки недоступны (детали в detail)"
    result = ToolResult(ok=ok, summary=summary or "руки доступны",
                        detail=detail, reason="" if ok else summary)
    _journal("health", started, result, {})
    return result


def main() -> None:
    import anyio

    anyio.run(server.run_stdio_async)


if __name__ == "__main__":
    main()
