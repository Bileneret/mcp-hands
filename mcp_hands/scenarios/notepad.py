"""Сценарий «Блокнот»: нативный путь — открыть, ввести текст, сохранить, проверить файл.

Правила, каждое из которых оплачено ошибкой на живом стенде:

1. **Работаем только со своей вкладкой.** Windows 11 Блокнот — пакетное приложение,
   открывающее файлы ВКЛАДКАМИ в одном процессе. Вкладка опознаётся по уникальному имени
   нашего файла, активируется выбором через UIA, и только после проверки заголовка
   выполняется ввод.
2. **Закрытие вкладки — строго по протоколу Ctrl+W (аудит v9).** Кнопки закрытия у
   WinUI-вкладки в дереве UIA нет, InvokePattern тоже, поэтому одними паттернами
   закрыть вкладку технически невозможно. Прежняя версия жала Ctrl+W без проверки —
   при неудачной активации нажатие уходило в чужое приложение. Утверждённый протокол:
   активация окна, подтверждение заголовка (имя нашего файла в заголовке = активна
   именно наша вкладка), затем Ctrl+W через стража expect_hwnd, постусловие — вкладка
   исчезла из дерева UIA, вкладки пользователя остались прежними.
3. **Любой ввод — только после подтверждения активного окна.** `Actuator` получает
   `expect_hwnd` и отказывается работать, если активно не оно.
4. **Перевод строки** отправляется клавишей Enter: юникод-символ `\\n` WinUI-Блокнот
   игнорирует, строки склеиваются.
5. **Сравнение текста** нормализует переводы строк (CR у документа, LF в наших строках,
   CRLF в файлах).
"""

from __future__ import annotations

import re
import subprocess
import time
import uuid
from pathlib import Path

from ..events import EVENT_OBJECT_SHOW, EVENT_SYSTEM_FOREGROUND, EventPump
from ..geometry import CoordinateSpace, enable_dpi_awareness
from ..input import VK_CONTROL, VK_S, VK_W, Actuator, same_text
from ..uia import UiaClient, element_text, enum_windows, find_child_element, select_element
from .runner import Run

TEXT = "MCP-Hands: сценарий Блокнота\nСтрока вторая с кириллицей\n"
WAIT_WINDOW_S = 20
TAB_PREFIX = "mcp-hands-"
TAB_DEPTH = 10  # вкладки лежат глубже четырёх уровней: узкий обход их не видит
# Имя вкладки в дереве UIA заканчивается статусом документа: «…txt. Не изменено.».
# Статус отражает содержимое документа, а не вмешательство в чужие вкладки, поэтому
# при сверке пользовательских вкладок он отбрасывается.
STATUS_SUFFIX = re.compile(r"\. (?:Не изменено|Изменено)\.?$")
DIRTY_TITLE = "*"  # при несохранённых изменениях Блокнот ставит «*» перед именем в заголовке


def base_name(name: str) -> str:
    """Имя вкладки без суффикса статуса: «RESULTS.md. Не изменено.» → «RESULTS.md»."""
    return STATUS_SUFFIX.sub("", name)


def window_title(hwnd: int) -> str:
    return next((w["title"] for w in notepad_windows() if w["hwnd"] == hwnd), "")


def make_marker() -> str:
    return f"{TAB_PREFIX}{uuid.uuid4().hex[:10]}"


def notepad_windows() -> list[dict]:
    return [window for window in enum_windows() if window["exe"].lower().startswith("notepad")]


def find_our_window(marker: str, timeout_s: float = WAIT_WINDOW_S) -> dict:
    """Окно Блокнота, в заголовке которого имя нашего файла (заголовок = активная вкладка)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for window in notepad_windows():
            if marker.lower() in window["title"].lower():
                return window
        time.sleep(0.2)
    return {}


def tab_nodes(uia: UiaClient, hwnd: int) -> list[dict]:
    nodes = uia.walk(uia.window(hwnd), depth=TAB_DEPTH, scope="deep")["nodes"]
    return [node for node in nodes if node.get("control_type") == "UIA_TabItemControlTypeId"]


def tab_names(uia: UiaClient, hwnd: int) -> list[str]:
    return [node.get("name", "") for node in tab_nodes(uia, hwnd)]


def tab_element(uia: UiaClient, hwnd: int, marker: str):
    """Элемент вкладки с нашим именем; None, если вкладки нет или дерево недоступно."""
    try:
        node = next((item for item in tab_nodes(uia, hwnd)
                     if marker.lower() in (item.get("name") or "").lower()), None)
        if node is None:
            return None
        return find_child_element(uia.automation, uia.window(hwnd), name=node["name"]) or \
            _find_tab_by_name(uia, hwnd, node["name"])
    except Exception:  # noqa: BLE001 — окно закрылось во время поиска: нашей вкладки нет
        return None


def _find_tab_by_name(uia: UiaClient, hwnd: int, name: str):
    from comtypes.gen import UIAutomationClient as UIA

    condition = uia.automation.CreatePropertyCondition(UIA.UIA_NamePropertyId, name)
    found = uia.window(hwnd).FindFirst(UIA.TreeScope_Descendants, condition)
    return found if found else None


def select_our_tab(uia: UiaClient, hwnd: int, marker: str) -> bool:
    """Выбирает нашу вкладку паттерном SelectionItem; затем заголовок подтверждается."""
    element = tab_element(uia, hwnd, marker)
    if element is None:
        return False
    try:
        return bool(select_element(element))
    except Exception:  # noqa: BLE001
        return False


def close_tab(uia: UiaClient, actuator: Actuator, hwnd: int, marker: str) -> dict:
    """Закрывает нашу вкладку Ctrl+W строго по протоколу аудита v9.

    Тройной контроль:
    1. Активация окна и подтверждение заголовка: имя нашего файла обязано быть в
       заголовке окна (= активна именно наша вкладка). Если активна чужая — наша
       выбирается через UIA и заголовок подтверждается заново.
    2. Ctrl+W уходит только через стража (expect_hwnd): при смещении фокуса — отказ
       и ноль событий.
    3. Постусловие: вкладки больше нет в дереве UIA (два замера подряд — защита от
       случайного разрыва обхода).
    """
    steps = {"selected_via_uia": False, "title_ok": False, "hotkey_ok": False, "gone": False}
    if not actuator.activate(hwnd).get("ok"):
        steps["reason"] = "окно не удалось активировать — нажатие не отправлялось"
        return steps
    if find_our_window(marker, timeout_s=1).get("hwnd") != hwnd:
        if not select_our_tab(uia, hwnd, marker):
            steps["reason"] = "наша вкладка не найдена в дереве — нажатие не отправлялось"
            return steps
        steps["selected_via_uia"] = True
        time.sleep(0.5)
        steps["title_after_select"] = window_title(hwnd)
    if find_our_window(marker, timeout_s=3).get("hwnd") != hwnd:
        steps["reason"] = "заголовок окна не подтверждён — нажатие не отправлялось"
        return steps
    steps["title_ok"] = True
    # Dirty-вкладка (звёздочка в заголовке) вместо закрытия показала бы диалог
    # «Сохранить изменения?» — вкладка осталась бы, а диалог повис в чужом окне.
    # Файл с нашим префиксом — наш артефакт прогона, сохранение в него безопасно.
    if window_title(hwnd).startswith(DIRTY_TITLE):
        save = actuator.hotkey(VK_CONTROL, VK_S, expect_hwnd=hwnd)
        steps["save"] = save
        if not save.get("ok"):
            steps["reason"] = save.get("reason", "Ctrl+S отменён стражем — вкладка не закрывается")
            return steps
        deadline = time.time() + 5
        while time.time() < deadline and window_title(hwnd).startswith(DIRTY_TITLE):
            time.sleep(0.3)
        if window_title(hwnd).startswith(DIRTY_TITLE):
            steps["reason"] = "после Ctrl+S документ остался с несохранёнными изменениями"
            return steps
    hotkey = actuator.hotkey(VK_CONTROL, VK_W, expect_hwnd=hwnd)
    steps["hotkey"] = hotkey
    if not hotkey.get("ok"):
        steps["reason"] = hotkey.get("reason", "нажатие отменено стражем")
        return steps
    steps["hotkey_ok"] = True
    misses = 0
    deadline = time.time() + 5
    while time.time() < deadline and misses < 2:
        time.sleep(0.3)
        misses = misses + 1 if tab_element(uia, hwnd, marker) is None else 0
    steps["gone"] = misses >= 2
    if not steps["gone"]:
        steps["reason"] = "вкладка осталась в дереве после Ctrl+W"
    return steps


def all_tabs(uia: UiaClient) -> list[str]:
    tabs: list[str] = []
    for window in notepad_windows():
        try:
            tabs.extend(tab_names(uia, window["hwnd"]))
        except Exception:  # noqa: BLE001 — окно исчезло между снапшотом и обходом
            continue
    return tabs


def scenario(run: Run, workdir: Path) -> None:
    enable_dpi_awareness()
    actuator = Actuator(CoordinateSpace())
    uia = UiaClient()

    with run.step("убрать вкладки прошлых прогонов", expect="вкладок с нашим префиксом нет",
                  method="Ctrl+W по протоколу аудита v9: подтверждение заголовка, страж") as step:
        removed: list[str] = []
        refused: list[str] = []
        for window in notepad_windows():
            try:
                names = tab_names(uia, window["hwnd"])
            except Exception:  # noqa: BLE001 — окно исчезло между снапшотом и обходом
                continue
            for name in names:
                # Имя вкладки UIA со статусом: «mcp-hands-…txt. Не изменено.»; маркер
                # обязан быть без суффикса статуса и без точки на конце — иначе он
                # никогда не совпадёт с заголовком окна («…txt — Блокнот», без точки).
                marker = base_name(name).split()[0].rstrip("*.") if name.split() else ""
                if not marker.startswith(TAB_PREFIX):
                    continue
                result = close_tab(uia, actuator, window["hwnd"], marker)
                if result.get("gone"):
                    removed.append(name)
                else:
                    refused.append(f"{name}: {result.get('reason', 'закрытие не подтвердилось')} "
                                   f"[sel={result.get('selected_via_uia')}, "
                                   f"title_after={result.get('title_after_select')}]")
        left = [name for name in all_tabs(uia) if name.startswith(TAB_PREFIX)]
        step.details.update({"removed": removed, "left": left, "refused": refused})
        run.confirm(f"убрано {len(removed)}, осталось наших {len(left)}", ok=not left)
        if left:
            step.error = f"не убраны наши вкладки: {left}; отказы: {refused}"

    marker = make_marker()
    target = workdir / f"{marker}.txt"
    target.write_text("", encoding="utf-8")
    user_tabs_before = sorted(base_name(name) for name in all_tabs(uia)
                              if not name.startswith(TAB_PREFIX))

    process = None
    hwnd = 0
    with run.step("открыть наш файл в Блокноте", expect="наша вкладка активна",
                  method="запуск процесса + ожидание появления вкладки") as step:
        run.note(user_tabs_before=user_tabs_before)
        with EventPump(events=(EVENT_SYSTEM_FOREGROUND, EVENT_OBJECT_SHOW)) as pump:
            process = subprocess.Popen(["notepad.exe", str(target)])
            event = pump.wait(lambda e: marker.lower() in e.title.lower(), timeout_s=3)
            step.details["event"] = event.describe() if event else None
        window = find_our_window(marker)
        step.details["window"] = window
        run.confirm(f"окно {window.get('title', '?')!r} (pid {window.get('pid')}), "
                    f"событие: {'да' if event else 'нет'}", ok=bool(window))
        if not window:
            step.error = "вкладка с нашим именем файла не появилась — ввод отменён"
            return
        hwnd = window["hwnd"]

    with run.step("активировать нашу вкладку", expect="активна именно наша вкладка",
                  method="активация окна + проверка заголовка") as step:
        activation = actuator.activate(hwnd)
        step.details["activation"] = activation
        time.sleep(0.4)
        focused = find_our_window(marker, timeout_s=3)
        step.details["focused"] = focused
        run.confirm(f"заголовок окна {focused.get('title', '?')!r}", ok=bool(focused))
        if not focused:
            step.error = "наша вкладка не стала активной — ввод отменён"

    _root, info = uia.content_root(hwnd, jump=True)

    def reader() -> str:
        """Перечитывает документ: элемент, взятый до активации, устаревает."""
        fresh, _info = uia.content_root(hwnd, jump=True)
        return element_text(fresh)

    def focus_document() -> bool:
        """Ставит фокус в текст документа и подтверждает: иначе ввод уйдёт в никуда."""
        fresh, _info = uia.content_root(hwnd, jump=True)
        try:
            fresh.SetFocus()
        except Exception:  # noqa: BLE001
            return False
        time.sleep(0.2)
        try:
            return bool(fresh.CurrentHasKeyboardFocus)
        except Exception:  # noqa: BLE001
            return False

    with run.step("ввести текст", expect="документ содержит введённый текст",
                  method="SendInput Unicode + Enter с проверкой активного окна") as step:
        step.details["content_root"] = info
        focused = focus_document()
        step.details["focus_in_document"] = focused
        if not focused:
            step.error = "фокус не в документе — ввод отменён, чтобы не писать в чужое поле"
            return
        typing = actuator.type_text_verified(TEXT, reader, attempts=3, expect_hwnd=hwnd)
        observed = reader()
        step.details.update({"typing": typing, "observed": observed[:160]})
        ok = bool(typing.get("ok")) and same_text(observed, TEXT)
        run.confirm(f"в документе {len(observed)} знаков, совпадение: {same_text(observed, TEXT)}",
                    ok=ok)
        if not ok:
            step.error = typing.get("reason", "текст не совпал")

    saved_clean = False
    with run.step("сохранить (Ctrl+S)", expect="файл на диске содержит введённый текст",
                  method="хоткей в проверенно активное окно + чтение файла") as step:
        saved_keys = actuator.hotkey(VK_CONTROL, VK_S, expect_hwnd=hwnd)
        step.details["hotkey"] = saved_keys
        if not saved_keys.get("ok"):
            step.error = saved_keys.get("reason", "нажатие отменено стражем")
        deadline = time.time() + 10
        saved = ""
        while time.time() < deadline:
            time.sleep(0.3)
            saved = target.read_text(encoding="utf-8", errors="replace")
            if same_text(saved, TEXT):
                break
        step.details["file"] = {"path": str(target), "bytes": target.stat().st_size,
                                "content": saved[:160]}
        ok = same_text(saved, TEXT)
        saved_clean = ok
        run.confirm(f"{target.stat().st_size} байт, текст совпал: {ok}", ok=ok)
        if not ok:
            step.error = f"в файле нет введённого текста: {saved[:80]!r}"

    with run.step("закрыть нашу вкладку", expect="наша вкладка закрыта, вкладки пользователя целы",
                  method="Ctrl+W по протоколу аудита v9: заголовок, страж expect_hwnd, "
                         "постусловие UIA") as step:
        if not saved_clean:
            step.error = "вкладка может содержать несохранённые изменения — Ctrl+W не отправляется"
            return
        result = close_tab(uia, actuator, hwnd, marker)
        names = all_tabs(uia)
        user_after = sorted(base_name(name) for name in names if not name.startswith(TAB_PREFIX))
        # Постусловие защиты — «ни одна пользовательская вкладка не исчезла»:
        # наш Ctrl+W механически не может попасть в чужую вкладку (только при
        # подтверждённом заголовке с именем нашего файла). Появление чужих —
        # восстановление сессии Блокнота (живой факт: 4 → 12 между замерами,
        # аудит v10 и живой сеанс этапа 2): не вмешательство, фиксируем отдельно.
        disappeared = sorted(set(user_tabs_before) - set(user_after))
        appeared = sorted(set(user_after) - set(user_tabs_before))
        users_ok = not disappeared
        step.details.update({"close": result, "tabs_after": names,
                             "user_tabs_after": user_after,
                             "user_disappeared": disappeared, "user_appeared": appeared})
        run.confirm(f"вкладка закрыта: {result['gone']}; пользовательские: "
                    f"исчезли {len(disappeared)}, появились средой {len(appeared)}",
                    ok=result["gone"] and users_ok)
        if not result["gone"] or not users_ok:
            step.error = result.get("reason", "") or \
                f"исчезли пользовательские вкладки: {disappeared}"
        if process is not None:
            try:
                process.wait(timeout=2)  # процесс НЕ убиваем: в нём могут быть чужие вкладки
            except subprocess.TimeoutExpired:
                pass
