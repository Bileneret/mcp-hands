"""Сценарий «Блокнот»: нативный путь — открыть, ввести текст, сохранить, проверить файл.

Правила, каждое из которых оплачено ошибкой на живом стенде:

1. **Работаем только со своей вкладкой.** Windows 11 Блокнот — пакетное приложение,
   открывающее файлы ВКЛАДКАМИ в одном процессе. Вкладка опознаётся по уникальному имени
   нашего файла, активируется выбором через UIA, и только после проверки заголовка
   выполняется ввод.
2. **Никаких клавиш для закрытия вкладки.** Прежняя версия жала `Ctrl+W`; клавиши
   действуют на АКТИВНОЕ окно, и при неудачной активации нажатие уходило в чужое
   приложение (закрывало вкладки браузера и чужие программы). Теперь вкладка закрывается
   паттернами UIA: выбор + программное нажатие кнопки закрытия.
3. **Любой ввод — только после подтверждения активного окна.** `Actuator` получает
   `expect_hwnd` и отказывается работать, если активно не оно.
4. **Перевод строки** отправляется клавишей Enter: юникод-символ `\\n` WinUI-Блокнот
   игнорирует, строки склеиваются.
5. **Сравнение текста** нормализует переводы строк (CR у документа, LF в наших строках,
   CRLF в файлах).
"""

from __future__ import annotations

import subprocess
import time
import uuid
from pathlib import Path

from ..events import EVENT_OBJECT_SHOW, EVENT_SYSTEM_FOREGROUND, EventPump
from ..geometry import CoordinateSpace, enable_dpi_awareness
from ..input import VK_CONTROL, VK_S, Actuator, same_text
from ..uia import (UiaClient, element_text, enum_windows, find_child_element, invoke_element,
                   select_element)
from .runner import Run

TEXT = "MCP-Hands: сценарий Блокнота\nСтрока вторая с кириллицей\n"
WAIT_WINDOW_S = 20
TAB_PREFIX = "mcp-hands-"
TAB_DEPTH = 10  # вкладки лежат глубже четырёх уровней: узкий обход их не видит


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
    """Элемент вкладки с нашим именем — для UIA-паттернов."""
    node = next((item for item in tab_nodes(uia, hwnd)
                 if marker.lower() in (item.get("name") or "").lower()), None)
    if node is None:
        return None
    return find_child_element(uia.automation, uia.window(hwnd), name=node["name"]) or \
        _find_tab_by_name(uia, hwnd, node["name"])


def _find_tab_by_name(uia: UiaClient, hwnd: int, name: str):
    from comtypes.gen import UIAutomationClient as UIA

    condition = uia.automation.CreatePropertyCondition(UIA.UIA_NamePropertyId, name)
    found = uia.window(hwnd).FindFirst(UIA.TreeScope_Descendants, condition)
    return found if found else None


def close_tab(uia: UiaClient, hwnd: int, marker: str) -> bool:
    """Закрывает нашу вкладку через UIA: выбор + программное нажатие кнопки закрытия.

    Ни одного нажатия клавиши: только паттерны. Так нажатие физически не может уйти
    в другое приложение.
    """
    element = tab_element(uia, hwnd, marker)
    if element is None:
        return True
    select_element(element)
    time.sleep(0.4)
    element = tab_element(uia, hwnd, marker)
    if element is None:
        return True
    button = find_child_element(uia.automation, element, control_type="button")
    if button is None or not invoke_element(button):
        return False
    time.sleep(0.6)
    return tab_element(uia, hwnd, marker) is None


def all_tabs(uia: UiaClient) -> list[str]:
    return [name for window in notepad_windows() for name in tab_names(uia, window["hwnd"])]


def scenario(run: Run, workdir: Path) -> None:
    enable_dpi_awareness()
    actuator = Actuator(CoordinateSpace())
    uia = UiaClient()

    with run.step("убрать вкладки прошлых прогонов", expect="вкладок с нашим префиксом нет",
                  method="UIA: выбор вкладки и нажатие её кнопки закрытия, без клавиш") as step:
        removed: list[str] = []
        for window in notepad_windows():
            for name in tab_names(uia, window["hwnd"]):
                marker = name.split()[0].rstrip("*") if name.split() else ""
                if marker.startswith(TAB_PREFIX) and close_tab(uia, window["hwnd"], marker):
                    removed.append(name)
        left = [name for name in all_tabs(uia) if name.startswith(TAB_PREFIX)]
        step.details.update({"removed": removed, "left": left})
        run.confirm(f"убрано {len(removed)}, осталось наших {len(left)}", ok=not left)

    marker = make_marker()
    target = workdir / f"{marker}.txt"
    target.write_text("", encoding="utf-8")
    user_tabs_before = [name for name in all_tabs(uia) if not name.startswith(TAB_PREFIX)]
    run.note(user_tabs_before=user_tabs_before)

    process = None
    hwnd = 0
    with run.step("открыть наш файл в Блокноте", expect="наша вкладка активна",
                  method="запуск процесса + ожидание появления вкладки") as step:
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
                  method="UIA: выбор вкладки + активация окна") as step:
        activation = actuator.activate(hwnd)
        element = tab_element(uia, hwnd, marker)
        selected = select_element(element) if element is not None else False
        step.details.update({"activation": activation, "selected_via_uia": selected})
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
        run.confirm(f"{target.stat().st_size} байт, текст совпал: {ok}", ok=ok)
        if not ok:
            step.error = f"в файле нет введённого текста: {saved[:80]!r}"

    with run.step("закрыть нашу вкладку", expect="наша вкладка закрыта, вкладки пользователя целы",
                  method="UIA: выбор вкладки и нажатие кнопки закрытия, без клавиш") as step:
        closed = close_tab(uia, hwnd, marker)
        names = all_tabs(uia)
        user_after = [name for name in names if not name.startswith(TAB_PREFIX)]
        step.details.update({"tabs_after": names, "user_tabs_after": user_after})
        run.confirm(f"вкладок осталось {len(names)}; пользовательские целы: "
                    f"{user_after == user_tabs_before}",
                    ok=closed and user_after == user_tabs_before)
        if process is not None:
            try:
                process.wait(timeout=2)  # процесс НЕ убиваем: в нём могут быть чужие вкладки
            except subprocess.TimeoutExpired:
                pass
