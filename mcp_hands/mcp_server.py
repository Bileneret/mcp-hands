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

import ctypes
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
    "ocr_find", "observe", "window_activate", "click_node", "type_text", "hotkey",
    "notepad_run", "word_pdf_run", "browser_links_run", "health",
    "fs_trash_move", "fs_sort_by_type",
    "word_report_from_template", "word_format_document", "excel_workbook_build",
    "browser_tabs_audit", "browser_collect_sources",
    "browser_extract_text", "word_docx_to_pdf", "browser_video_capture",
    "live_browser", "live_navigate", "live_page", "live_search",
    "live_click_text", "live_video", "live_screenshot", "live_youtube"})

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


def filter_windows(rows: list[dict], query: str = "") -> list[dict]:
    """Отбор окон по подстроке в заголовке или имени процесса (регистр не важен).

    Точечный вопрос («открыт ли Блокнот?») не должен зависеть от того, сколько всего
    окон в системе: с `query` в ответ идёт только совпавшее, и усечение больших
    списков его не съедает (находка аудита v23).
    """
    needle = query.strip().lower()
    if not needle:
        return rows
    return [row for row in rows
            if needle in str(row.get("title", "")).lower()
            or needle in str(row.get("exe", "")).lower()]


@server.tool(structured_output=True)
def windows_list(query: str = "") -> ToolResult:
    """Список верхнеуровневых окон (hwnd, pid, процесс, заголовок, прямоугольник).

    `query` — фильтр по подстроке в заголовке или процессе: «открыт ли Блокнот?»
    решается одним вызовом и не зависит от числа остальных окон.
    """
    started = time.perf_counter()
    try:
        from .uia import enum_windows

        rows = enum_windows()
    except Exception as exc:  # noqa: BLE001
        return _fail("windows_list", started, f"{type(exc).__name__}: {exc}")
    matched = filter_windows(rows, query)
    summary = f"окон: {len(rows)}"
    if query.strip():
        summary = (f"по запросу {query.strip()!r}: {len(matched)} из {len(rows)} окон")
    result = ToolResult(ok=True, summary=summary,
                        detail={"windows": matched, "total": len(rows),
                                "query": query.strip(), "found": len(matched)})
    _journal("windows_list", started, result, {"query": query})
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
        # «Не найдено» — не сбой и не отказ: инструмент отработал, элемента нет.
        # Но и не «ок» без пояснений: модель должна читать это как отсутствие элемента,
        # а не как успех (в живом прогоне владельца ok=True без found путало вывод).
        result = ToolResult(ok=True, refused=False,
                            summary=f"НЕ НАЙДЕНО: элемента "
                                    f"{(name or control_type or automation_id)!r} в окне {hwnd} нет",
                            detail={"found": False, "status": "not_found",
                                    "query": {"name": name, "control_type": control_type,
                                              "automation_id": automation_id}})
        _journal("element_find", started, result, {"hwnd": hwnd, "name": name})
        return result
    found = {"ok": True, "summary": f"найдено: {node.get('name', '')!r}",
             "refused": False, "reason": "",
             "detail": {"found": True, "status": "found", "node": node}}
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
def content_map(hwnd: int, depth: int = 3, query: str = "") -> ToolResult:
    """Карта дерева окна (UIA): тип, имя, automation_id, прямоугольник; до depth уровней.

    `query` — фильтр по имени/automation_id: в дереве Chromium-окон тысячи узлов,
    и без фильтра ответ тонет. Обход таких окон нередко отваливается на уровне COM —
    это состояние окна, а не поломка инструмента, поэтому отказ приходит с советом.
    """
    started = time.perf_counter()
    try:
        _ensure_com()
        client = _automation()
        walked = client.walk(client.window(hwnd), depth=depth, scope="deep")
        nodes = walked.get("nodes", [])
    except Exception as exc:  # noqa: BLE001 — дерево недоступно: это вердикт, не сбой
        return _fail("content_map", started,
                     f"дерево окна недоступно ({type(exc).__name__}: {exc}). "
                     "У окон Chromium/Electron обход дерева часто отваливается: "
                     "используй element_find с именем/типом или ocr_find по тексту — "
                     "они работают точечно и не зависят от полного обхода", hwnd=hwnd)
    needle = query.strip().lower()
    matched = [node for node in nodes
               if not needle or needle in str(node.get("name", "")).lower()
               or needle in str(node.get("automation_id", "")).lower()]
    limit = 600
    shown = matched[:limit]
    summary = f"узлов: {len(shown)}"
    if needle:
        summary = f"по запросу {query.strip()!r}: {len(matched)} из {len(nodes)} узлов"
    elif len(matched) > limit:
        summary = (f"узлов: {len(shown)} (обрезано из {len(matched)}; "
                   "уточни query — выгружать всё дерево бессмысленно)")
    result = ToolResult(ok=True, summary=summary,
                        detail={"count": len(shown), "matched": len(matched),
                                "total": len(nodes), "query": query.strip(),
                                "nodes": shown})
    _journal("content_map", started, result, {"hwnd": hwnd, "depth": depth, "query": query})
    return result


def _screens_word_lines(words: list) -> list[dict]:
    """Склейка слов OCR в строки: 40 строк читаются моделью лучше, чем 209 слов.

    Усечение словарного списка съедало самое главное (в логе владельца модель
    получила только ярлыки вкладок), поэтому рядом со словами едут строки —
    компактно и без потери смысла.
    """
    lines: dict[int, list] = {}
    for word in words:
        lines.setdefault(word.line_index, []).append(word)
    result: list[dict] = []
    for index in sorted(lines):
        row = sorted(lines[index], key=lambda item: item.rect["left"])
        result.append({"text": " ".join(item.text for item in row).strip(),
                       "y": min(item.rect["top"] for item in row),
                       "x": min(item.rect["left"] for item in row)})
    return [line for line in result if line["text"]]


def _ocr_frame(hwnd: int = 0):
    """Кадр для OCR: весь экран (hwnd=0) или видимая часть окна. Возвращает (image, origin)."""
    grabber = _screen()
    frame = grabber.grab(timeout_s=2.0)
    if not hwnd:
        return frame.image, (frame.origin[0], frame.origin[1])
    crop = grabber.crop_window(_coordinate_space(), hwnd, frame)
    return crop.image, (crop.origin[0], crop.origin[1])


@server.tool(structured_output=True)
def ocr_window(hwnd: int = 0, query: str = "", title_substring: str = "") -> ToolResult:
    """OCR окна: кроп видимой части + встроенный OCR Windows; строки и слова.

    Окно называется дескриптором ИЛИ заголовком (`title_substring`) — планы
    не обязаны знать внутренние числа, а неоднозначность даёт честный отказ.

    В ответе — строки целиком (читаемо), первые слова и признак перекрытия:
    кадр читается с ЭКРАНА, поэтому поверх окна могло лечь чужое — об этом честно
    сообщается, вместо того чтобы выдавать чужой текст за содержимое окна.
    """
    started = time.perf_counter()
    from .geometry import window_occlusion
    from .screen import ScreenCaptureError

    hwnd, why = resolve_window(hwnd, title_substring)
    if not hwnd:
        return _fail("ocr_window", started, why, True,
                     hwnd=hwnd, title_substring=title_substring)
    try:
        image, origin = _ocr_frame(hwnd)
        words = _ocr().recognize(image, offset=origin)
    except ScreenCaptureError as exc:
        return _fail("ocr_window", started, f"захват экрана отказал: {exc}",
                     refused=True, hwnd=hwnd)
    except Exception as exc:  # noqa: BLE001
        return _fail("ocr_window", started, f"{type(exc).__name__}: {exc}", hwnd=hwnd)
    needle = query.strip().lower()
    picked = [word for word in words if not needle or needle in word.text.lower()]
    rows = [word.to_dict() for word in picked]
    lines = _screens_word_lines(picked)
    occlusion = window_occlusion(hwnd)
    summary = f"слов: {len(rows)}, строк: {len(lines)}"
    if needle:
        summary = f"по запросу {query.strip()!r}: слов {len(rows)} из {len(words)}"
    if occlusion.get("covered"):
        summary += (f"; ВНИМАНИЕ: окно перекрыто ({int(occlusion['share'] * 100)}% "
                    f"площади) — в кадре видно чужое окно")
    result = ToolResult(ok=True, summary=summary,
                        detail={"lines": lines[:80], "line_count": len(lines),
                                # координаты нужны только при точечном поиске; полный
                                # список слов дублировал бы строки и съедал бюджет
                                "words": rows[:40] if needle else [],
                                "word_count": len(rows), "total_words": len(words),
                                "query": query.strip(), "occlusion": occlusion,
                                "ms": round(_ocr().last_ms or 0.0, 1)})
    _journal("ocr_window", started, result, {"hwnd": hwnd, "query": query})
    return result


@server.tool(structured_output=True)
def observe(max_lines: int = 25) -> ToolResult:
    """Компактный срез обстановки: «что вокруг прямо сейчас» одним вызовом.

    Активное окно (заголовок, процесс, перекрытие), строки текста в нём, сколько
    всего окон, состояние живой сессии браузера и что из восприятия доступно.
    Это разведка перед планом цели: вместо серии догадок модель получает факты и
    уже по ним строит шаги. Ответ держится в бюджете ~3 КБ.
    """
    started = time.perf_counter()
    from .geometry import window_occlusion

    facts: dict = {}
    try:
        from .uia import enum_windows

        user32 = ctypes.windll.user32
        hwnd = int(user32.GetForegroundWindow())
        windows = enum_windows()
        row = next((item for item in windows if int(item["hwnd"]) == hwnd), {})
        active = {"hwnd": hwnd, "title": row.get("title", ""), "exe": row.get("exe", ""),
                  "occlusion": window_occlusion(hwnd) if hwnd else {}}
        facts = {"active": active, "windows_total": len(windows)}
    except Exception as exc:  # noqa: BLE001 — обстановку обязан уметь доложить каждый режим
        return _fail("observe", started, f"{type(exc).__name__}: {exc}")
    lines: list[dict] = []
    if facts.get("active", {}).get("hwnd"):
        try:
            image, origin = _ocr_frame(int(facts["active"]["hwnd"]))
            words = _ocr().recognize(image, offset=origin)
            lines = _screens_word_lines(words)[:max_lines]
        except Exception as exc:  # noqa: BLE001 — без кадра живут и остальные факты
            facts["lines_note"] = f"текст окна не прочитан: {type(exc).__name__}"
    facts["lines"] = lines
    browser = _live()
    facts["browser"] = {"mode": browser.mode, "port": browser.port,
                        "tab_url": (browser.tab or {}).get("url", ""),
                        "tab_title": (browser.tab or {}).get("title", "")}
    from .ocr import available as ocr_available

    facts["capabilities"] = {"ocr": bool(ocr_available()), "live": bool(browser.mode),
                             "lines_shown": len(lines)}
    while len(json.dumps(facts, ensure_ascii=False, default=str)) > 3000 and facts["lines"]:
        facts["lines"] = facts["lines"][: max(1, len(facts["lines"]) // 2)]
        facts["capabilities"]["lines_shown"] = len(facts["lines"])
    title = facts["active"]["title"][:60]
    summary = f"активное окно: {title or '—'}"
    blocked = facts["active"]["occlusion"]
    if blocked.get("covered"):
        who = (blocked.get("by") or [{}])[0].get("title", "")
        summary += f" (перекрыто: {who[:40]})"
    result = ToolResult(ok=True, refused=False, summary=summary, detail=facts)
    _journal("observe", started, result, {})
    return result


@server.tool(structured_output=True)
def ocr_find(text: str, hwnd: int = 0, exact: bool = False,
             limit: int = 12) -> ToolResult:
    """Есть ли текст на экране (или в окне) и где именно — прямой ответ на «видишь X?».

    Без `hwnd` читается весь экран, с `hwnd` — видимая часть окна. Ответ короткий:
    найдено/нет, сколько вхождений, координаты первых и текст строк, где нашлось.
    Если окно перекрыто другим, это отдельный факт: содержимое кадра в этом случае
    чужое, и подменять им реальность нельзя.
    """
    started = time.perf_counter()
    from .geometry import window_occlusion
    from .ocr import WindowsOcr
    from .screen import ScreenCaptureError

    needle = text.strip()
    if not needle:
        return _fail("ocr_find", started, "пустой запрос: что искать?", True)
    try:
        image, origin = _ocr_frame(hwnd)
        words = _ocr().recognize(image, offset=origin)
    except ScreenCaptureError as exc:
        return _fail("ocr_find", started, f"захват экрана отказал: {exc}",
                     refused=True, hwnd=hwnd)
    except Exception as exc:  # noqa: BLE001
        return _fail("ocr_find", started, f"{type(exc).__name__}: {exc}", hwnd=hwnd)
    matches = WindowsOcr.find(words, needle, exact=exact)
    found_rows = [word.to_dict() for word in matches][:limit]
    context: list[dict] = []
    if matches:
        by_line: dict[int, list] = {}
        for word in words:
            by_line.setdefault(word.line_index, []).append(word)
        for index in sorted({word.line_index for word in matches})[:6]:
            row = sorted(by_line.get(index, []), key=lambda item: item.rect["left"])
            line_text = " ".join(item.text for item in row).strip()
            if line_text:
                context.append({"text": line_text,
                                "y": min(item.rect["top"] for item in row)})
    occlusion = window_occlusion(hwnd) if hwnd else {}
    detail = {"found": bool(matches), "count": len(matches), "matches": found_rows,
              "lines": context[:6], "words_in_frame": len(words),
              "scope": f"окно {hwnd}" if hwnd else "весь экран",
              "ms": round(_ocr().last_ms or 0.0, 1)}
    if occlusion:
        detail["occlusion"] = occlusion
    if matches:
        first = matches[0]
        centre = first.center
        summary = (f"найдено {len(matches)} вхождений {needle!r}; "
                   f"первое — {centre[0]}x{centre[1]}")
    else:
        summary = (f"НЕ НАЙДЕНО: {needle!r} "
                   f"({'в окне' if hwnd else 'на экране'}) — слов в кадре: {len(words)}")
        if occlusion.get("covered"):
            summary += f"; окно перекрыто: {occlusion.get('by', [{}])[0].get('title', '')[:40]}"
    result = ToolResult(ok=True, refused=False, summary=summary, detail=detail)
    _journal("ocr_find", started, result, {"text": needle, "hwnd": hwnd})
    return result


# --- действия ---------------------------------------------------------------------


def resolve_window(hwnd: int = 0, title_substring: str = "") -> tuple[int, str]:
    """Находит окно по дескриптору ИЛИ по подстроке в заголовке (внутренний помощник).

    Планировщик не знает числовых дескрипторов, и это нормально: в живом прогоне
    19.10 модель пыталась активировать окно, не имея hwnd, и получала отказ.
    Теперь окно можно назвать по заголовку — как это и делает человек. Возвращает
    (hwnd, причина): hwnd=0 означает «не найдено или неоднозначно», и причина
    объясняет, что именно случилось.
    """
    if hwnd:
        return int(hwnd), ""
    needle = title_substring.strip()
    if not needle:
        return 0, "нужен hwnd или title_substring"
    from .uia import enum_windows

    rows = filter_windows(enum_windows(), needle)
    if not rows:
        return 0, f"окно с заголовком {needle!r} не найдено"
    exact = [row for row in rows if needle.lower() in str(row.get("title", "")).lower()]
    if len(exact) > 1:
        titles = [f"{row['title'][:40]!r} (hwnd {row['hwnd']})" for row in exact[:4]]
        return 0, (f"под {needle!r} подходит {len(exact)} окон: {titles} — "
                   "уточни формулировку или передай hwnd")
    return int(exact[0]["hwnd"]), ""


@server.tool(structured_output=True)
def window_activate(hwnd: int = 0, title_substring: str = "") -> ToolResult:
    """Активация окна (<1.5 с) — SetForegroundWindow + страховка AttachThreadInput.

    Окно можно назвать заголовком (`title_substring`) вместо дескриптора: так
    строятся планы, не знающие внутренних чисел. Неоднозначность — честный отказ
    со списком кандидатов, а не активация «первого попавшегося».
    """
    started = time.perf_counter()
    hwnd, why = resolve_window(hwnd, title_substring)
    if not hwnd:
        return _fail("window_activate", started, why, True,
                     hwnd=hwnd, title_substring=title_substring)
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


# --- офисные инструменты (колонна A: Word-COM композиции) -------------------------


@server.tool(structured_output=True)
def word_report_from_template(template: str, replacements: dict,
                              save_as: str) -> ToolResult:
    """Собрать документ по ШАБЛОНУ: открыть шаблон → заменить плейсхолдеры → сохранить как.

    Титульный лист и структура живут в самом шаблоне (литералами), инструмент
    подменяет реквизиты и подтверждает: файл создан, размер не нулевой,
    плейсхолдеров в документе не осталось (проверка — состоянием документа,
    не кодом возврата). Word поднимается и закрывается внутри вызова.
    """
    started = time.perf_counter()
    from pathlib import Path as _Path

    if not _Path(template).is_file():
        return _fail("word_report_from_template", started,
                     f"шаблон не найден: {template}", True)
    if not replacements:
        return _fail("word_report_from_template", started, "нет замен", True)
    from .office import WordSession
    from .probe import DOMAIN_OPERATION_TIMEOUT_S, ProbeProcess

    word = None
    try:
        with ProbeProcess(default_timeout_s=DOMAIN_OPERATION_TIMEOUT_S) as satellite:
            word = WordSession(satellite)
            start = word.start()
            if not start.ok:
                return _fail("word_report_from_template", started,
                             f"Word не поднялся: {start.reason}")
            opened = word.open(template)
            if not opened.ok:
                return _fail("word_report_from_template", started,
                             f"шаблон не открылся: {opened.reason}")
            replaced = word.replace(replacements)
            if not (replaced.ok and replaced.value.get("replaced_all")):
                return _fail("word_report_from_template", started,
                             f"замены не полны: остались {replaced.value.get('remaining')}")
            saved = word.save_as(save_as)
            target = _Path(save_as)
            exists = target.exists()
            size = target.stat().st_size if exists else 0
            outcome_ok = saved.ok and exists and size > 0
            detail = {"template": template, "saved": str(target), "bytes": size,
                      "replace": replaced.value, "save_outcome": saved.describe()}
    except Exception as exc:  # noqa: BLE001
        return _fail("word_report_from_template", started, f"{type(exc).__name__}: {exc}")
    finally:
        # свой экземпляр Word закрывается всегда: у пользователя может быть свой
        if word is not None and word.pid:
            try:
                word.quit()
            except Exception:  # noqa: BLE001
                pass
    result = ToolResult(ok=outcome_ok,
                        reason="" if outcome_ok else "документ не сохранён",
                        summary=f"отчёт собран: {save_as} ({detail['bytes']} байт)",
                        detail=detail)
    _journal("word_report_from_template", started, result, {"template": template})
    return result


@server.tool(structured_output=True)
def word_format_document(path: str, actions: list, save_as: str = "") -> ToolResult:
    """Форматирование документа Word: стили, шрифты, интервалы, оглавление.

    `actions` — список под-операций: `{"op": "set_style", "find": "Введение",
    "style": "Heading 1"}`, `{"op": "set_font", "find": ..., "name": "Times New Roman",
    "size": 14}`, `{"op": "set_spacing", "line_spacing": 1.5, "space_after": 6}`,
    `{"op": "insert_toc", "find": "ОГЛАВЛЕНИЕ"}`. Каждая под-операция подтверждается
    перечитыванием свойства документа (стиль/шрифт/интервалы/число оглавлений).
    `save_as` пустой — сохранить в исходный файл (перезапись THIS документа).
    """
    started = time.perf_counter()
    from pathlib import Path as _Path

    if not _Path(path).is_file():
        return _fail("word_format_document", started, f"документ не найден: {path}", True)
    if not actions:
        return _fail("word_format_document", started, "пустой список операций", True)
    from .office import WordSession
    from .probe import DOMAIN_OPERATION_TIMEOUT_S, ProbeProcess

    word = None
    try:
        with ProbeProcess(default_timeout_s=DOMAIN_OPERATION_TIMEOUT_S) as satellite:
            word = WordSession(satellite)
            start = word.start()
            if not start.ok:
                return _fail("word_format_document", started,
                             f"Word не поднялся: {start.reason}")
            opened = word.open(path)
            if not opened.ok:
                return _fail("word_format_document", started,
                             f"документ не открылся: {opened.reason}")
            formatted = word.format_document(actions)
            if not (formatted.ok and (formatted.value or {}).get("all_ok")):
                bad = [item for item in (formatted.value or {}).get("applied", [])
                       if not item.get("ok")]
                reason = formatted.reason or f"под-операции отклонились: {bad[:3]}"
                return _fail("word_format_document", started, reason)
            target = save_as or path
            saved = word.save_as(target)
            size = _Path(target).stat().st_size if _Path(target).exists() else 0
            outcome_ok = saved.ok and size > 0
            detail = {"file": str(target), "bytes": size,
                      "format": formatted.value, "save_outcome": saved.describe()}
    except Exception as exc:  # noqa: BLE001
        return _fail("word_format_document", started, f"{type(exc).__name__}: {exc}")
    finally:
        if word is not None and word.pid:
            try:
                word.quit()
            except Exception:  # noqa: BLE001
                pass
    result = ToolResult(ok=outcome_ok,
                        reason="" if outcome_ok else "документ не сохранён",
                        summary=(f"отформатировано; оглавлений: "
                                 f"{detail['format'].get('toc_count')} ({detail['bytes']} байт)"),
                        detail=detail)
    _journal("word_format_document", started, result, {"path": path})
    return result


# --- офисные инструменты (колонна A: Word-композиции, Excel) ---------------------


def _values_equal(left, right) -> bool:
    """Сверка ячейки Excel с исходным значением CSV: числа — численно, текст — точно."""
    if left is None and right is None:
        return True
    if isinstance(left, (int, float)) and not isinstance(left, bool):
        try:
            return abs(float(left) - float(str(right).replace(",", "."))) < 1e-9
        except (TypeError, ValueError):
            return False
    return str(left).strip() == str(right).strip()


@server.tool(structured_output=True)
def excel_workbook_build(inputs: list[str], output: str, sheet_name: str = "Свод",
                         chart: dict | None = None) -> ToolResult:
    """Свести данные из CSV-файлов в книгу Excel и, при заказе, построить диаграмму.

    `inputs` — CSV (первая строка файла — заголовок; повторные заголовки следующих
    файлов пропускаются). `output` — путь .xlsx. `chart` — `{"type": "column"|"line"|
    "pie", "title": "..."}` по диапазону данных. Постусловие — сверка С ДИСКА:
    книга закрывается и открывается заново, значения ячеек сличаются с исходными
    данными, число диаграмм подтверждается. Excel поднимается и закрывается внутри.
    """
    started = time.perf_counter()
    import csv
    from pathlib import Path as _Path

    files = [_Path(item) for item in inputs]
    missing = [str(f) for f in files if not f.is_file()]
    if missing:
        return _fail("excel_workbook_build", started, f"нет CSV: {missing}", True)
    if not inputs:
        return _fail("excel_workbook_build", started, "пустой список inputs", True)

    rows: list[list] = []
    for index, path in enumerate(files):
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            parsed = [row for row in csv.reader(handle) if any(cell.strip() for cell in row)]
        if not parsed:
            return _fail("excel_workbook_build", started, f"{path.name}: пустой файл", True)
        if index == 0:
            rows.extend(parsed)
        else:
            header, *data = parsed
            if header != rows[0]:
                return _fail("excel_workbook_build", started,
                             f"{path.name}: заголовок {header} не совпадает с первым файлом",
                             True)
            rows.extend(data)
    height, width = len(rows), max(len(row) for row in rows)

    from .office import ExcelSession
    from .probe import DOMAIN_OPERATION_TIMEOUT_S, ProbeProcess

    excel = None
    try:
        with ProbeProcess(default_timeout_s=DOMAIN_OPERATION_TIMEOUT_S) as satellite:
            excel = ExcelSession(satellite)
            start = excel.start()
            if not start.ok:
                return _fail("excel_workbook_build", started,
                             f"Excel не поднялся: {start.reason}")
            created = excel.new_workbook(sheet_name)
            if not created.ok:
                return _fail("excel_workbook_build", started,
                             f"книга не создана: {created.reason}")
            filled = excel.fill("A1", rows)
            written_info = filled.value or {}
            if not filled.ok or written_info.get("rows") != height \
                    or written_info.get("written", 0) <= 0:
                return _fail("excel_workbook_build", started,
                             f"данные записаны неполно: {filled.value} / ожидалось "
                             f"{height} строк")
            chart_info = {}
            if chart:
                source_range = f"A1:{chr(ord('A') + width - 1)}{height}"
                kind = {"column": 51, "line": 4, "pie": 5}.get(str(chart.get("type", "column")), 51)
                drawn = excel.add_chart(source_range, chart_type=kind,
                                        title=str(chart.get("title", "")))
                if not drawn.ok:
                    return _fail("excel_workbook_build", started,
                                 f"диаграмма не построена: {drawn.reason}")
                chart_info = drawn.value or {}
            saved = excel.save_as(output)
            target = _Path(output)
            exists = target.exists()
            size = target.stat().st_size if exists else 0
            if not (saved.ok and exists and size > 0):
                return _fail("excel_workbook_build", started,
                             f"книга не сохранена: {saved.reason}")
            # Постусловие: читаем С ДИСКА — закрыть книгу и открыть заново
            excel.close_workbook()
            reopened = excel.open(output)
            if not reopened.ok:
                return _fail("excel_workbook_build", started,
                             f"книга не открылась заново: {reopened.reason}")
            last_cell = f"{chr(ord('A') + width - 1)}{height}"
            read = excel.read(f"A1:{last_cell}")
            if not read.ok:
                return _fail("excel_workbook_build", started,
                             f"не удалось перечитать книгу: {read.reason}")
            values = (read.value or {}).get("values") or []
            mismatches = []
            for row_index, row in enumerate(rows):
                for col_index, expected in enumerate(row):
                    got = values[row_index][col_index] if row_index < len(values) \
                        and col_index < len(values[row_index]) else None
                    if not _values_equal(got, expected):
                        mismatches.append({"cell": f"{chr(ord('A') + col_index)}{row_index + 1}",
                                           "expected": expected, "got": got})
            charts_on_disk = int((read.value or {}).get("charts_on_sheet", 0))
            detail = {"file": str(target), "bytes": size, "rows": height, "cols": width,
                      "cells_checked": height * width, "mismatches": mismatches[:5],
                      "charts_on_disk": charts_on_disk, "chart": chart_info,
                      "save_outcome": saved.describe()}
            ok = not mismatches and (charts_on_disk >= 1 if chart else True)
            reason = ""
            if mismatches:
                reason = f"рассогласование ячеек: {mismatches[:3]}"
            elif chart and charts_on_disk < 1:
                reason = "диаграмма не сохранилась на диск"
    except Exception as exc:  # noqa: BLE001
        return _fail("excel_workbook_build", started, f"{type(exc).__name__}: {exc}")
    finally:
        if excel is not None and excel.pid:
            try:
                excel.quit()
            except Exception:  # noqa: BLE001
                pass
    result = ToolResult(ok=ok, reason=reason,
                        summary=(f"книга {detail['rows']}×{detail['cols']}, "
                                 f"диаграмм {detail['charts_on_disk']}, "
                                 f"сверка с диска: {'ок' if not detail['mismatches'] else 'есть расхождения'}"),
                        detail=detail)
    _journal("excel_workbook_build", started, result, {"files": len(files)})
    return result


# --- браузерные инструменты (колонна C: свой headless-браузер, локальные страницы) ---


@server.tool(structured_output=True)
def browser_tabs_audit(urls: list[str], output: str, keep: list[str] | None = None,
                       close_extras: bool = True) -> ToolResult:
    """Разбор сессии вкладок: открыть заданные URL в СВОЁМ headless-браузере,
    сгруппировать по домену, сохранить отчёт и (по умолчанию) закрыть лишнее.

    `keep` — какие вкладки оставить (по умолчанию: уникальные из `urls`).
    Постусловие: файл-отчёт на диске; после закрытия на отладочном порту
    остались ровно вкладки из `keep` (перечитывание списка ПОСЛЕ действия).
    Чужой браузер не затрагивается: всё в своём профиле каталога прогона.
    """
    started = time.perf_counter()
    if not urls:
        return _fail("browser_tabs_audit", started, "пустой список urls", True)
    from pathlib import Path as _Path

    from .browser import tabs_audit

    profile_root = _Path(output).parent / "browser_profile"
    outcome = tabs_audit(urls, output, keep=keep, close_extras=close_extras,
                         profile_root=profile_root)
    result = ToolResult(ok=bool(outcome.get("ok")), refused=False,
                        reason=outcome.get("reason", ""),
                        summary=(f"вкладок открыто {len(outcome.get('opened', []))}, "
                                 f"закрыто {len(outcome.get('closed', []))}, "
                                 f"осталось {len(outcome.get('remaining', []))}"),
                        detail=outcome)
    _journal("browser_tabs_audit", started, result, {"urls": len(urls)})
    return result


@server.tool(structured_output=True)
def browser_collect_sources(url: str, output_csv: str) -> ToolResult:
    """Собрать источники со страницы-стенда в CSV (автор, год, ссылка, заголовок).

    Контракт страницы: карточки `article.source` (ссылка `a[href]`, автор `.author`,
    год в `data-year`). Постусловие: CSV на диске, и повторный подсчёт карточек
    в DOM ПОСЛЕ записи совпадает с числом строк CSV.
    """
    started = time.perf_counter()
    from pathlib import Path as _Path

    from .browser import collect_sources

    profile_root = _Path(output_csv).parent / "browser_profile"
    outcome = collect_sources(url, output_csv, profile_root=profile_root)
    result = ToolResult(ok=bool(outcome.get("ok")), refused=False,
                        reason=outcome.get("reason", ""),
                        summary=f"собрано источников: {outcome.get('rows', 0)}",
                        detail=outcome)
    _journal("browser_collect_sources", started, result, {"url": url})
    return result


@server.tool(structured_output=True)
def browser_extract_text(url: str, output_docx: str) -> ToolResult:
    """Текст статьи со страницы → документ Word с оформлением цитаты.

    Заголовок и ссылка оформляются абзацем-цитатой (стиль Quote), ниже — текст
    статьи. Постусловие L1: файл непустой, стиль цитаты применён (InUse);
    независимое подтверждение L2 (zipfile/XML) делает runner после шага.
    """
    started = time.perf_counter()
    from pathlib import Path as _Path

    from .browser import extract_article
    from .office import WordSession
    from .probe import DOMAIN_OPERATION_TIMEOUT_S, ProbeProcess

    profile_root = _Path(output_docx).parent / "browser_profile"
    article = extract_article(url, profile_root=profile_root)
    if not article.get("ok"):
        return _fail("browser_extract_text", started,
                     f"статья не собрана: {article.get('reason', '')}", True)
    quote = f"{article['title']}\nИсточник: {article['url']}"
    body = f"{quote}\n\n{article['text']}"
    word = None
    try:
        with ProbeProcess(default_timeout_s=DOMAIN_OPERATION_TIMEOUT_S) as satellite:
            word = WordSession(satellite)
            if not word.start().ok:
                return _fail("browser_extract_text", started, "Word не поднялся")
            created = word.new_document(body)
            if not created.ok:
                return _fail("browser_extract_text", started,
                             f"документ не создан: {created.reason}")
            formatted = word.format_document([
                {"op": "set_style", "find": article["title"], "style": "Quote"}])
            if not (formatted.ok and (formatted.value or {}).get("all_ok")):
                return _fail("browser_extract_text", started,
                             f"стиль цитаты не применён: {formatted.reason or formatted.value}")
            saved = word.save_as(output_docx)
            target = _Path(output_docx)
            size = target.stat().st_size if target.exists() else 0
            outcome_ok = saved.ok and size > 0
            detail = {"file": str(target), "bytes": size, "title": article["title"],
                      "text_chars": len(article["text"]),
                      "format": formatted.value, "save_outcome": saved.describe()}
    except Exception as exc:  # noqa: BLE001
        return _fail("browser_extract_text", started, f"{type(exc).__name__}: {exc}")
    finally:
        if word is not None and word.pid:
            try:
                word.quit()
            except Exception:  # noqa: BLE001
                pass
    result = ToolResult(ok=outcome_ok, reason="" if outcome_ok else "документ не сохранён",
                        summary=f"статья {detail['text_chars']} знаков → {output_docx} "
                                f"({detail['bytes']} байт)",
                        detail=detail)
    _journal("browser_extract_text", started, result, {"url": url})
    return result


@server.tool(structured_output=True)
def word_docx_to_pdf(path: str, output_pdf: str) -> ToolResult:
    """Экспорт готового документа Word в PDF (открыть → ExportAsFixedFormat).

    Кирпич для составных задач (b10): подтверждение — PDF на диске и его текст
    читается независимым парсером (pypdf).
    """
    started = time.perf_counter()
    from pathlib import Path as _Path

    if not _Path(path).is_file():
        return _fail("word_docx_to_pdf", started, f"документ не найден: {path}", True)
    from .office import WordSession
    from .probe import DOMAIN_OPERATION_TIMEOUT_S, ProbeProcess

    word = None
    try:
        with ProbeProcess(default_timeout_s=DOMAIN_OPERATION_TIMEOUT_S) as satellite:
            word = WordSession(satellite)
            if not word.start().ok:
                return _fail("word_docx_to_pdf", started, "Word не поднялся")
            opened = word.open(path)
            if not opened.ok:
                return _fail("word_docx_to_pdf", started,
                             f"документ не открылся: {opened.reason}")
            exported = word.export_pdf(output_pdf)
            target = _Path(output_pdf)
            size = target.stat().st_size if target.exists() else 0
            text_chars = 0
            if size > 0:
                try:
                    from pypdf import PdfReader

                    reader = PdfReader(str(target))
                    text_chars = sum(len(page.extract_text() or "") for page in reader.pages)
                except Exception as exc:  # noqa: BLE001 — битый PDF = результат
                    return _fail("word_docx_to_pdf", started, f"PDF нечитаем: {exc}")
            outcome_ok = exported.ok and size > 0 and text_chars > 0
            detail = {"file": str(target), "bytes": size, "text_chars": text_chars,
                      "export_outcome": exported.describe()}
    except Exception as exc:  # noqa: BLE001
        return _fail("word_docx_to_pdf", started, f"{type(exc).__name__}: {exc}")
    finally:
        if word is not None and word.pid:
            try:
                word.quit()
            except Exception:  # noqa: BLE001
                pass
    result = ToolResult(ok=outcome_ok,
                        reason="" if outcome_ok else "PDF не создан или пуст",
                        summary=f"PDF {detail['bytes']} байт, знаков {detail['text_chars']}",
                        detail=detail)
    _journal("word_docx_to_pdf", started, result, {"path": path})
    return result


@server.tool(structured_output=True)
def browser_video_capture(url: str, output_json: str, output_notes: str,
                          playback_rate: float = 1.5) -> ToolResult:
    """Задача «медиа» на ЛОКАЛЬНОМ стенде: плеер, скорость, таймкоды → заметки.

    Контракт страницы: `video#player` (длительность в `data-duration`), реплики
    в `#transcript .cue` (`data-start`/`data-end`), заголовок в `h1`.
    Инструмент выставляет `playbackRate`, выписывает таймкоды в заметки и JSON.
    Постусловия: заметки содержат каждый таймкод; фактическая скорость страницы
    равна заказанной (перечитывание ПОСЛЕ установки). Внешний веб не используется.
    """
    started = time.perf_counter()
    from pathlib import Path as _Path

    from .browser import video_capture

    profile_root = _Path(output_json).parent / "browser_profile"
    outcome = video_capture(url, output_json, output_notes, playback_rate=playback_rate,
                            profile_root=profile_root)
    result = ToolResult(ok=bool(outcome.get("ok")), refused=False,
                        reason=outcome.get("reason", ""),
                        summary=(f"таймкодов {outcome.get('cues', 0)} → заметки "
                                 f"({outcome.get('notes_file', '')})"),
                        detail=outcome)
    _journal("browser_video_capture", started, result, {"url": url})
    return result


# --- живые сценарии (настоящий браузер по CDP: Opera GX, живой веб) ---------------

_live_browser = None


def _live():
    """Одна живая сессия на процесс сервера (рабочий или пользовательский профиль)."""
    global _live_browser
    if _live_browser is None:
        from .live import LiveBrowser

        _live_browser = LiveBrowser()
    return _live_browser


@server.tool(structured_output=True)
def live_browser(action: str = "start", profile: str = "work",
                 headless: bool = True) -> ToolResult:
    """Живой браузер (Opera GX) под CDP: start (свой профиль) / connect_user
    (ЖИВАЯ Opera владельца с отладочным портом — запускается его лончером;
    наш сервер её не перезапускает и не закрывает) / stop / status.

    Управление протоколом: навигация, поиск, DOM-клики, видео, полный экран,
    скриншот. Браузер владельца без отладочного порта не трогается вовсе.
    """
    started = time.perf_counter()
    browser = _live()
    try:
        if action == "start":
            if profile == "user":
                outcome = browser.connect_user()
            else:
                outcome = {**browser.start_work(headless=headless), "ok": True}
                try:
                    browser.ensure_tab()
                except Exception:
                    # не оставляем осиротевший экземпляр: если подключение к вкладке
                    # не удалось, наш процесс всё равно закрывается
                    browser.stop()
                    raise
        elif action == "connect_user":
            outcome = browser.connect_user()
        elif action == "stop":
            outcome = {**browser.stop(), "ok": True}
        elif action == "status":
            outcome = {"ok": True, **browser.status()}
        else:
            return _fail("live_browser", started, f"неизвестное действие {action!r}", True)
    except Exception as exc:  # noqa: BLE001
        return _fail("live_browser", started, f"{type(exc).__name__}: {exc}")
    ok = bool(outcome.get("ok", True))
    mode = outcome.get("mode") or (profile if action == "start" else "")
    page = outcome.get("title") or outcome.get("url") or outcome.get("note", "")
    result = ToolResult(ok=ok, refused=False, reason=outcome.get("reason", ""),
                        summary=f"живой браузер: {action}"
                                + (f" ({mode})" if mode else "")
                                + (f" — {page[:70]}" if page else ""),
                        detail=outcome)
    _journal("live_browser", started, result, {"action": action, "profile": profile})
    return result


def _live_result(tool: str, started: float, outcome: dict, summary: str) -> ToolResult:
    result = ToolResult(ok=bool(outcome.get("ok")), refused=False,
                        reason=outcome.get("reason", ""), summary=summary,
                        detail=outcome)
    _journal(tool, started, result, {})
    return result


@server.tool(structured_output=True)
def live_navigate(url: str) -> ToolResult:
    """Перейти по адресу в живом браузере (реальный сайт)."""
    started = time.perf_counter()
    try:
        outcome = _live().navigate(url)
    except Exception as exc:  # noqa: BLE001
        return _fail("live_navigate", started, f"{type(exc).__name__}: {exc}")
    return _live_result("live_navigate", started, {**outcome, "ok": bool(outcome.get("title"))},
                        f"открыто: {outcome.get('title', '')[:60]}")


@server.tool(structured_output=True)
def live_page() -> ToolResult:
    """Что сейчас на странице живого браузера: адрес, заголовок, поисковые поля."""
    started = time.perf_counter()
    try:
        outcome = _live().page_info() or {}
    except Exception as exc:  # noqa: BLE001
        return _fail("live_page", started, f"{type(exc).__name__}: {exc}")
    return _live_result("live_page", started, {**outcome, "ok": bool(outcome.get("url"))},
                        f"{outcome.get('title', '')[:50]} | {outcome.get('url', '')[:60]}")


@server.tool(structured_output=True)
def live_search(query: str) -> ToolResult:
    """Напечатать запрос в поиске ТЕКУЩЕЙ страницы и подтвердить выдачу."""
    started = time.perf_counter()
    try:
        outcome = _live().search(query)
    except Exception as exc:  # noqa: BLE001
        return _fail("live_search", started, f"{type(exc).__name__}: {exc}")
    return _live_result("live_search", started, outcome,
                        f"выдача: {outcome.get('found', 0)} карточек")


@server.tool(structured_output=True)
def live_click_text(text: str) -> ToolResult:
    """DOM-клик по элементу с заданным текстом (не координатный «слепой» клик)."""
    started = time.perf_counter()
    try:
        outcome = _live().click_text(text)
    except Exception as exc:  # noqa: BLE001
        return _fail("live_click_text", started, f"{type(exc).__name__}: {exc}")
    return _live_result("live_click_text", started, outcome,
                        f"клик: {outcome.get('text', '')[:60]}")


@server.tool(structured_output=True)
def live_video(action: str = "status") -> ToolResult:
    """Видео на странице: status / play / pause / fullscreen / exit — с фактами."""
    started = time.perf_counter()
    try:
        outcome = _live().video(action)
    except Exception as exc:  # noqa: BLE001
        return _fail("live_video", started, f"{type(exc).__name__}: {exc}")
    summary = (f"{action}: играет={not outcome.get('paused', True)}"
               f", полный экран={outcome.get('fullscreen', False)}"
               f", {outcome.get('current_time', 0)}/{outcome.get('duration')} с")
    return _live_result("live_video", started, outcome, summary)


@server.tool(structured_output=True)
def live_screenshot(path: str = "") -> ToolResult:
    """Скриншот страницы живого браузера (PNG)."""
    started = time.perf_counter()
    from pathlib import Path as _Path

    target = path or str(_Path(__file__).resolve().parents[1] / "logs" / "live" /
                         time.strftime("shot_%Y%m%d_%H%M%S.png"))
    try:
        outcome = _live().screenshot(target)
    except Exception as exc:  # noqa: BLE001
        return _fail("live_screenshot", started, f"{type(exc).__name__}: {exc}")
    return _live_result("live_screenshot", started, outcome,
                        f"кадр: {outcome.get('bytes', 0)} байт")


@server.tool(structured_output=True)
def live_youtube(query: str, fullscreen: bool = True,
                 screenshot_path: str = "") -> ToolResult:
    """Живой сценарий «видео по запросу»: YouTube → поиск → первое видео →
    воспроизведение → (по флагу) полный экран → (по флагу) скриншот.

    Постусловия фактами: видео существует, `paused == false` (играет),
    при `fullscreen=True` — `document.fullscreenElement` не пуст. Пер-шаговые
    записи в detail показывают, где именно не сложилось.
    """
    started = time.perf_counter()
    browser = _live()
    steps: list[dict] = []
    try:
        if not browser.mode:
            steps.append({"step": "start", **browser.start_work(headless=True)})
            browser.ensure_tab()
        elif not browser.tab:
            steps.append({"step": "tab", **browser.ensure_tab()})
        nav = browser.navigate("https://www.youtube.com")
        steps.append({"step": "navigate", "url": nav.get("url"), "title": nav.get("title")})
        time.sleep(1.5)
        found = browser.search(query)
        steps.append({"step": "search", "found": found.get("found"),
                      "top": (found.get("top") or [{}])[0].get("text", "")[:80],
                      "ok": found.get("ok")})
        if not found.get("ok"):
            return _fail("live_youtube", started, f"выдача пуста: {found.get('reason')}")
        clicked = browser.open_first_video()
        steps.append({"step": "open_video", "ok": clicked.get("ok"),
                      "how": clicked.get("how"), "text": clicked.get("text", "")[:80]})
        if not clicked.get("ok"):
            return _fail("live_youtube", started,
                         f"видео не открылось: {clicked.get('reason')}")
        appeared = browser.wait_for("!!document.querySelector('video')", 25.0)
        steps.append({"step": "video_appeared", "ok": appeared})
        if not appeared:
            return _fail("live_youtube", started, "видео-элемент не появился")
        played = browser.video("play")
        steps.append({"step": "play", "paused": played.get("paused"),
                      "duration": played.get("duration")})
        if played.get("paused"):
            return _fail("live_youtube", started, "видео не начало играть")
        if fullscreen:
            full = browser.video("fullscreen")
            steps.append({"step": "fullscreen", "fullscreen": full.get("fullscreen"),
                          "current_time": full.get("current_time")})
        shot = None
        if screenshot_path:
            shot = browser.screenshot(screenshot_path)
            steps.append({"step": "screenshot", "bytes": shot.get("bytes")})
        status = browser.video("status")
        ok = (not status.get("paused")) and (not fullscreen or status.get("fullscreen"))
        outcome = {"ok": ok, "steps": steps, "title": status.get("title", "")[:100],
                   "playing": not status.get("paused"), "fullscreen": status.get("fullscreen"),
                   "position_s": status.get("current_time"), "duration_s": status.get("duration")}
        if shot:
            outcome["screenshot"] = shot.get("file")
        result = ToolResult(ok=ok, refused=False, reason="" if ok else "постусловия не выполнены",
                            summary=(f"видео играет: {outcome['playing']}, полный экран: "
                                     f"{outcome['fullscreen']}, {outcome['position_s']}/"
                                     f"{outcome['duration_s']} с"),
                            detail=outcome)
        _journal("live_youtube", started, result, {"query": query})
        return result
    except Exception as exc:  # noqa: BLE001 — обрыв связи и прочие отказы: вердикт, не падение
        return _fail("live_youtube", started, f"{type(exc).__name__}: {exc}")
    finally:
        pass  # живая сессия живёт до явного live_browser(stop); в user-режиме не закрываем


# --- файлы (колонна B: политика «только корзина») -------------------------------


@server.tool(structured_output=True)
def fs_trash_move(paths: list[str], dry_run: bool = False) -> ToolResult:
    """Перемещает файлы/папки в КОРЗИНУ (`SHFileOperation` + `FOF_ALLOWUNDO`).

    Полного удаления в наборе не существует. Запрещены системные области
    (SystemRoot, Program Files, ProgramData, корни дисков) — отказ до действий.
    `dry_run=True` — только проверка политики и существования, без изменений.
    """
    started = time.perf_counter()
    if not paths:
        return _fail("fs_trash_move", started, "пустой список путей", True)
    from .files import FilesPolicyError, trash

    try:
        if dry_run:
            from pathlib import Path as _Path

            from .files import ensure_allowed

            checked = []
            for item in paths:
                try:
                    resolved = ensure_allowed(item)
                except FilesPolicyError as exc:
                    return _fail("fs_trash_move", started, str(exc), True)
                checked.append({"path": str(resolved), "exists": resolved.exists()})
            result = ToolResult(ok=all(c["exists"] for c in checked), refused=False,
                                summary=f"сухой прогон: в корзину ушли бы {len(checked)} путей",
                                detail={"dry_run": True, "checked": checked})
            _journal("fs_trash_move", started, result, {"count": len(paths), "dry_run": True})
            return result
        outcome = trash(paths)
    except FilesPolicyError as exc:
        return _fail("fs_trash_move", started, str(exc), True)
    except Exception as exc:  # noqa: BLE001
        return _fail("fs_trash_move", started, f"{type(exc).__name__}: {exc}")
    result = ToolResult(ok=bool(outcome.get("ok")), refused=False,
                        reason=outcome.get("reason", ""),
                        summary=f"в корзину отправлено: {len(outcome.get('trashed', []))} "
                                f"(предупреждений: {len(outcome.get('warnings', []))})",
                        detail=outcome)
    _journal("fs_trash_move", started, result, {"count": len(paths)})
    return result


@server.tool(structured_output=True)
def fs_sort_by_type(source: str, target_root: str, dry_run: bool = False) -> ToolResult:
    """Разбор папки по типам: файлы верхнего уровня раскладываются по категориям
    (документы/изображения/архивы/аудио/видео/прочее) в подпапки цели.

    Никаких перезаписей: совпадение имён разрешается суффиксом. `dry_run` —
    план перемещений без единого изменения. Постусловие боевого прогона:
    в источнике не осталось файлов.
    """
    started = time.perf_counter()
    from .files import FilesPolicyError, sort_by_type

    try:
        outcome = sort_by_type(source, target_root, dry_run=dry_run)
    except FilesPolicyError as exc:
        return _fail("fs_sort_by_type", started, str(exc), True)
    except Exception as exc:  # noqa: BLE001
        return _fail("fs_sort_by_type", started, f"{type(exc).__name__}: {exc}")
    result = ToolResult(ok=bool(outcome.get("ok")), refused=False,
                        reason=outcome.get("reason", ""),
                        summary=(f"сухой прогон: к перемещению {outcome.get('count', 0)} файлов"
                                 if dry_run else
                                 f"перемещено {len(outcome.get('moved', []))} файлов"),
                        detail=outcome)
    _journal("fs_sort_by_type", started, result, {"source": source, "dry_run": dry_run})
    return result


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
