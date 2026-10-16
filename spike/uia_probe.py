"""UIA-зонд: один обход дерева окна с замерами. Может работать как дочерний процесс зонда.

Режимы обхода (--mode):
  cached      — один FindAllBuildCache(Children, cache=Subtree) + клиентский спуск
                по GetCachedChildren (штатный режим, см. заметку о семантике ниже);
  perparent   — FindAllBuildCache на каждом родителе: столько же переходов, сколько
                родителей (контраст к cached, показывает цену лишних переходов);
  naive       — FindAll без кэша + чтение Current*: каждое свойство = межпроцессный
                вызов (антипример, нужен для доказательства пользы кэш-реквеста);
  descendants — один FindAllBuildCache(TreeScope_Descendants) без ограничения глубины.

Семантика кэш-реквеста (выяснена замерами, отличается от «наивного» чтения документации):
область запроса отсчитывается от КАЖДОГО элемента результата, а не от корня поиска.
Поэтому связка «search=Children, cache=Children» отдаёт контейнеры, у которых чтение
Cached* падает с E_INVALIDARG (0x80070057), а «search=Children, cache=Subtree» — элементы
со свойствами, чьи потомки уже лежат в кэше: спуск через GetCachedChildren не делает
новых межпроцессных вызовов. Ограничение глубины при этом применяется на нашей стороне.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dpi_aware  # noqa: E402
import uia_common  # noqa: E402
from comtypes.gen import UIAutomationClient as UIA  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="UIA-зонд: обход дерева окна с замерами")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--exe", help="имя процесса окна, например Notepad.exe")
    target.add_argument("--hwnd", help="дескриптор окна, например 0x1F02A4")
    target.add_argument("--pid", type=int, help="PID процесса, берётся его первое видимое окно")
    parser.add_argument("--title", default="", help="подстрока заголовка (уточняет --exe)")
    parser.add_argument("--depth", type=int, default=3, help="глубина обхода")
    parser.add_argument("--mode", choices=("cached", "perparent", "naive", "descendants", "propcost"),
                        default="cached")
    parser.add_argument("--profile", choices=("base", "action", "full"), default="action",
                        help="набор свойств в кэш-реквесте")
    parser.add_argument("--patterns", action="store_true",
                        help="добавить в кэш сами паттерны (замер стоимости AddPattern)")
    parser.add_argument("--limit", type=int, default=400,
                        help="максимум узлов в JSON-дампе (на счётчик элементов не влияет)")
    parser.add_argument("--connection-timeout-ms", type=int, default=None,
                        help="IUIAutomation2.ConnectionTimeout (таймаут чужого провайдера)")
    parser.add_argument("--json", help="путь для JSON-результата")
    parser.add_argument("--jump", choices=("none", "auto", "document", "render_widget"), default="none",
                        help="сначала прыгнуть к корню контента, а не обходить от окна")
    parser.add_argument("--dump", type=int, default=None,
                        help="напечатать сводку и дерево из первых N узлов (0 — только сводку)")
    parser.add_argument("--indent", action="store_true", help="отступы по уровням в дереве")
    return parser.parse_args()


def resolve_content_root(uia, window_element, window_hwnd: int, jump: str) -> tuple[object, dict]:
    """Корень контента окна: render-widget (Chromium), Document или само окно.

    Обход от верхнего окна до страницы не доходит: у Chromium до контента 8+ уровней
    хрома браузера, а у Electron активной ветки accessibility может не быть вовсе.

    `auto` = видимый render-widget → Document → само окно. Тупик «Document без детей»
    (WinUI-текстовое поле Блокнота) вызывающий откатывает обходом от окна.
    """
    if jump == "none":
        return window_element, {"how": "window"}

    if jump in ("render_widget", "auto"):
        started = time.perf_counter()
        candidates = [child for child in uia_common.child_windows(window_hwnd)
                      if "RenderWidgetHost" in child["class_name"] and child["visible"]
                      and child["width"] > 0 and child["height"] > 0]
        pick_ms = (time.perf_counter() - started) * 1000.0
        if candidates:
            widest = max(candidates, key=lambda child: child["width"] * child["height"])
            return uia.ElementFromHandle(widest["hwnd"]), {
                "how": "render_widget",
                "hwnd": hex(widest["hwnd"]),
                "class_name": widest["class_name"],
                "rect": widest["rect"],
                "candidates": len(candidates),
                "ms": round(pick_ms, 2),
            }

    if jump in ("document", "auto"):
        condition = uia.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId,
                                                UIA.UIA_DocumentControlTypeId)
        started = time.perf_counter()
        document = window_element.FindFirst(UIA.TreeScope_Descendants, condition)
        find_ms = (time.perf_counter() - started) * 1000.0
        if document:
            return document, {"how": "document", "ms": round(find_ms, 2)}

    return window_element, {"how": "window", "note": "прыжок не нашёл корень контента"}


def is_empty_root(levels: list[dict]) -> bool:
    """Корень контента оказался листом: у Document в WinUI-Блокноте детей нет.

    В этом случае вызывающий откатывается к обходу от самого окна.
    """
    return bool(levels) and levels[0]["elements"] == 0


def resolve_window(args) -> dict:
    if args.hwnd:
        hwnd = int(args.hwnd, 16) if args.hwnd.lower().startswith("0x") else int(args.hwnd)
        for item in uia_common.enum_windows():
            if item["hwnd"] == hwnd:
                return item
        return {"hwnd": hwnd, "pid": 0, "exe": "", "title": ""}

    if args.pid:
        for item in uia_common.enum_windows():
            if item["pid"] == args.pid:
                return item
        raise SystemExit(f"нет видимого окна у процесса pid={args.pid}")

    candidates = uia_common.find_windows(args.exe, args.title)
    if not candidates:
        raise SystemExit(f"окно процесса {args.exe!r} не найдено (title={args.title!r})")
    return max(candidates, key=_title_weight)


def _title_weight(item: dict) -> tuple[int, int]:
    """Берём самое «содержательное» окно процесса: сначала с заголовком, потом самое широкое."""
    try:
        rect = uia_common.window_rect(item["hwnd"])
        area = max(0, rect["right"] - rect["left"]) * max(0, rect["bottom"] - rect["top"])
    except OSError:
        area = 0
    return (1 if item["title"] else 0, area)


def walk_cached(uia, window_element, depth: int, profile: str, with_patterns: bool,
                limit: int) -> tuple[list[dict], list[dict], int]:
    """Один межпроцессный переход + клиентский спуск по кэшу (штатный режим)."""
    cache = uia_common.make_cache(uia, UIA.TreeScope_Subtree, profile, with_patterns)
    condition = uia.CreateTrueCondition()

    started = time.perf_counter()
    array = window_element.FindAllBuildCache(UIA.TreeScope_Children, condition, cache)
    first_level = [array.GetElement(index) for index in range(array.Length)]
    call_ms = (time.perf_counter() - started) * 1000.0

    levels = [_level_row(1, 1, len(first_level), call_ms)]
    elements_by_level = [first_level]

    current = first_level
    for level in range(2, depth + 1):
        started = time.perf_counter()
        children: list = []
        for parent in current:
            children.extend(uia_common.cached_children(parent))
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        levels.append(_level_row(level, len(current), len(children), elapsed_ms))
        if not children:
            break
        elements_by_level.append(children)
        current = children

    nodes, props_read = _nodes(elements_by_level, profile, limit)
    return levels, nodes, props_read


def walk_perparent(uia, window_element, depth: int, profile: str, with_patterns: bool,
                   limit: int) -> tuple[list[dict], list[dict], int]:
    """Переход на каждого родителя: контраст, показывающий цену лишних переходов."""
    levels: list[dict] = []
    elements_by_level: list[list] = []
    condition = uia.CreateTrueCondition()
    current = [window_element]

    for level in range(1, depth + 1):
        cache = uia_common.make_cache(uia, UIA.TreeScope_Subtree, profile, with_patterns)
        started = time.perf_counter()
        children: list = []
        for parent in current:
            array = parent.FindAllBuildCache(UIA.TreeScope_Children, condition, cache)
            for index in range(array.Length):
                children.append(array.GetElement(index))
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        levels.append(_level_row(level, len(current), len(children), elapsed_ms))
        if not children:
            break
        elements_by_level.append(children)
        current = children

    nodes, props_read = _nodes(elements_by_level, profile, limit)
    return levels, nodes, props_read


def walk_naive(uia, window_element, depth: int, profile: str,
               limit: int) -> tuple[list[dict], list[dict], int]:
    """Обход без кэша: FindAll + чтение Current*-свойств (каждое — переход в чужой процесс)."""
    levels: list[dict] = []
    elements_by_level: list[list] = []
    condition = uia.CreateTrueCondition()
    current = [window_element]

    for level in range(1, depth + 1):
        started = time.perf_counter()
        children: list = []
        for parent in current:
            array = parent.FindAll(UIA.TreeScope_Children, condition)
            for index in range(array.Length):
                children.append(array.GetElement(index))
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        levels.append(_level_row(level, len(current), len(children), elapsed_ms))
        if not children:
            break
        elements_by_level.append(children)
        current = children

    nodes: list[dict] = []
    for level_index, elements in enumerate(elements_by_level, start=1):
        for element in elements:
            node = uia_common.node_from_current(element, profile)
            node["level"] = level_index
            nodes.append(node)
    return levels, nodes, len(nodes)


def walk_descendants(uia, window_element, profile: str, with_patterns: bool,
                     limit: int) -> tuple[list[dict], list[dict], int]:
    """Один запрос на всё поддерево — без ограничения глубины («сколько стоит не ограничивать»)."""
    cache = uia_common.make_cache(uia, UIA.TreeScope_Subtree, profile, with_patterns)
    condition = uia.CreateTrueCondition()
    started = time.perf_counter()
    array = window_element.FindAllBuildCache(UIA.TreeScope_Descendants, condition, cache)
    count = array.Length
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    nodes = []
    for index in range(min(count, limit)):
        node = uia_common.node_from_cached(array.GetElement(index), profile)
        node["level"] = 0  # уровень в плоском списке потомков не определён
        nodes.append(node)

    return [_level_row("all", 1, count, elapsed_ms)], nodes, len(nodes)


def walk_propcost(uia, window_element, depth: int, profile: str, with_patterns: bool,
                  limit: int) -> tuple[list[dict], list[dict], int]:
    """Честная цена кэша: один и тот же набор элементов, свойства из кэша против Current*.

    Обходы в режимах cached/naive собирают разные наборы (Chromium отдаёт разное число
    узлов на разной глубине), поэтому «наивный» выглядит быстрее просто потому, что
    элементов меньше. Здесь набор фиксируется заранее и меряется только чтение свойств.
    """
    cache = uia_common.make_cache(uia, UIA.TreeScope_Subtree, profile, with_patterns)
    condition = uia.CreateTrueCondition()

    array = window_element.FindAllBuildCache(UIA.TreeScope_Children, condition, cache)
    current = [array.GetElement(index) for index in range(array.Length)]
    elements: list = []
    for _ in range(1, depth + 1):
        elements.extend(current)
        children: list = []
        for parent in current:
            children.extend(uia_common.cached_children(parent))
        if not children:
            break
        current = children

    property_count = len(uia_common.PROFILE_PROPERTIES[profile])

    started = time.perf_counter()
    cached_nodes = [uia_common.node_from_cached(element, profile) for element in elements]
    cached_ms = (time.perf_counter() - started) * 1000.0

    started = time.perf_counter()
    current_nodes: list[dict] = []
    for element in elements:
        try:
            current_nodes.append(uia_common.node_from_current(element, profile))
        except Exception:  # noqa: BLE001 — у части элементов Current* недоступен
            continue
    current_ms = (time.perf_counter() - started) * 1000.0

    levels = [
        {"level": account, "parents": len(elements), "elements": len(elements),
         "ms": round(elapsed, 3), "ms_per_parent": round(elapsed / max(1, len(elements)), 4),
         "properties_per_element": property_count}
        for account, elapsed in (("cached_read", cached_ms), ("current_read", current_ms))
    ]
    for node in cached_nodes:
        node["level"] = "cached_read"
    for node in current_nodes:
        node["level"] = "current_read"
    return levels, cached_nodes + current_nodes, len(elements)


def _level_row(level, parents: int, elements: int, elapsed_ms: float) -> dict:
    return {
        "level": level,
        "parents": parents,
        "elements": elements,
        "ms": round(elapsed_ms, 2),
        "ms_per_parent": round(elapsed_ms / max(1, parents), 2),
    }


def _nodes(elements_by_level: list[list], profile: str, limit: int) -> tuple[list[dict], int]:
    """Читает свойства ВСЕХ собранных элементов, в JSON отдаёт первые `limit`.

    Так сравнение режимов остаётся честным: счётчик прочитанных свойств не зависит
    от ограничения на размер дампа.
    """
    nodes: list[dict] = []
    for level_index, elements in enumerate(elements_by_level, start=1):
        for element in elements:
            node = uia_common.node_from_cached(element, profile)
            node["level"] = level_index
            nodes.append(node)
    return nodes, len(nodes)


def _run_walk(uia, args, element) -> tuple[list[dict], list[dict], int]:
    """Выбирает стратегию обхода по --mode."""
    if args.mode == "cached":
        return walk_cached(uia, element, args.depth, args.profile, args.patterns, args.limit)
    if args.mode == "perparent":
        return walk_perparent(uia, element, args.depth, args.profile, args.patterns, args.limit)
    if args.mode == "naive":
        return walk_naive(uia, element, args.depth, args.profile, args.limit)
    if args.mode == "descendants":
        return walk_descendants(uia, element, args.profile, args.patterns, args.limit)
    return walk_propcost(uia, element, args.depth, args.profile, args.patterns, args.limit)


def main() -> int:
    args = parse_args()
    dpi_mode = dpi_aware.enable()
    uia = uia_common.create_uia(args.connection_timeout_ms)

    target = resolve_window(args)
    stage = "element_from_handle"
    find_ms = 0.0
    jump_info: dict = {"how": "none"}
    levels: list[dict] = []
    nodes: list[dict] = []
    props_read = 0
    error = None
    started = time.perf_counter()

    try:
        element = uia.ElementFromHandle(target["hwnd"])
        find_ms = (time.perf_counter() - started) * 1000.0
        if element is None:
            raise SystemExit(f"ElementFromHandle вернул None для hwnd={target['hwnd']:#x}")

        stage = "content_root"
        window_element = element
        element, jump_info = resolve_content_root(uia, element, target["hwnd"], args.jump)

        stage = "walk"
        started = time.perf_counter()
        levels, nodes, props_read = _run_walk(uia, args, element)
        if jump_info["how"] != "window" and is_empty_root(levels):
            # Document оказался листом (WinUI-текстовое поле) — обходим от окна.
            jump_info = {**jump_info, "fallback": "document без детей, обход от окна"}
            element = window_element
            levels, nodes, props_read = _run_walk(uia, args, element)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — на зависшем провайдере UIA бросает 0x800705B4
        if not find_ms:
            # вызов ElementFromHandle не вернулся: время до исключения и есть время блокировки
            find_ms = (time.perf_counter() - started) * 1000.0
        error = f"{stage}: {type(exc).__name__}: {exc}"
    total_ms = (time.perf_counter() - started) * 1000.0

    if args.mode == "descendants":
        total_elements = levels[0]["elements"]
    elif args.mode == "propcost":
        total_elements = props_read
    else:
        total_elements = sum(level["elements"] for level in levels)

    result = {
        "target": {**target, "hwnd": hex(target["hwnd"]), "rect": _safe_rect(target["hwnd"])},
        "mode": args.mode,
        "profile": args.profile,
        "with_patterns": bool(args.patterns),
        "depth": args.depth,
        "dpi_awareness": dpi_mode,
        "connection_timeout_ms": args.connection_timeout_ms,
        "ms_element_from_handle": round(find_ms, 2),
        "content_root": jump_info,
        "levels": levels,
        "total_elements": total_elements,
        "props_read": props_read,
        "nodes_dumped": min(len(nodes), args.limit),
        "ms_walk": round(total_ms, 2),
        "ms_total": round(total_ms, 2),
        "ms_per_element": round(total_ms / max(1, total_elements), 3),
        "error": error,
        "nodes": nodes[: args.limit],
    }

    if args.dump is not None:
        print(f"Цель: {target['exe']} pid={target['pid']} hwnd={target['hwnd']:#x} {target['title']!r}")
        print(f"Режим: {args.mode}, профиль {args.profile}, глубина {args.depth}, DPI {dpi_mode}")
        print(f"Корень контента: {jump_info}")
        for level in levels:
            print(f"  уровень {level['level']}: {level['elements']} элементов за {level['ms']} мс "
                  f"({level['ms_per_parent']} мс/родитель)")
        print(f"  итого: {total_elements} элементов, {total_ms:.1f} мс, "
              f"{total_ms / max(1, total_elements):.3f} мс/элемент")
        if args.dump:
            print()
            for node in nodes[: args.dump]:
                pad = "  " * (node.get("level", 0) if args.indent else 0)
                print("  " + pad + uia_common.node_brief(node))

    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    if not args.json and args.dump is None:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


def _safe_rect(hwnd: int) -> dict | None:
    try:
        return uia_common.window_rect(hwnd)
    except OSError:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
