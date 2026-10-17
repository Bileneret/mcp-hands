"""Проверка трёх утверждений внешнего аудита фактами.

1. Кэш UIA: аудитор утверждает, что правильная связка — `TreeScope_Element | TreeScope_Children`
   (0x3), а мой `Subtree` (0x5) заставляет провайдера кэшировать всё поддерево.
2. События: аудитор утверждает, что колбэк внепроцессного хука вызывается в нашем потоке
   и только внутри цикла сообщений; я наблюдал иное.
3. Геометрия окна: аудитор утверждает, что `GetWindowRect` включает невидимые рамки DWM
   (7-8 px) и нужен `DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS)`.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dpi_aware  # noqa: E402
import uia_common  # noqa: E402
from comtypes.gen import UIAutomationClient as UIA  # noqa: E402

SCOPE_ELEMENT = 0x1
SCOPE_CHILDREN = 0x2
SCOPE_SUBTREE = 0x5


def test_cache_scopes(window_title: str) -> None:
    """1. Какие связки областей дают читаемые свойства и какова их цена."""
    uia = uia_common.create_uia()
    windows = [w for w in uia_common.enum_windows() if window_title in w["title"]]
    if not windows:
        print(f"[1] окно {window_title!r} не найдено — пропуск")
        return
    hwnd = windows[0]["hwnd"]
    print(f"[1] окно: {windows[0]['exe']} {windows[0]['title'][:40]!r}")
    condition = uia.CreateTrueCondition()
    element = uia.ElementFromHandle(hwnd)

    for label, scope in (("Children (0x2)", SCOPE_CHILDREN),
                         ("Element|Children (0x3)", SCOPE_ELEMENT | SCOPE_CHILDREN),
                         ("Subtree (0x5)", SCOPE_SUBTREE)):
        cache = uia.CreateCacheRequest()
        cache.TreeScope = scope
        cache.AddProperty(UIA.UIA_NamePropertyId)
        cache.AddProperty(UIA.UIA_ControlTypePropertyId)
        started = time.perf_counter()
        try:
            array = element.FindAllBuildCache(SCOPE_CHILDREN, condition, cache)
            children = [array.GetElement(i) for i in range(array.Length)]
            call_ms = (time.perf_counter() - started) * 1000.0
        except Exception as exc:  # noqa: BLE001
            print(f"   cache={label:<22} поиск упал: {exc}")
            continue

        readable = 0
        sample = None
        for child in children[:5]:
            try:
                name = child.CachedName
                readable += 1
                if sample is None:
                    sample = f"{child.CachedControlType}:{name[:20]!r}"
            except Exception:  # noqa: BLE001
                pass

        # спуск на уровень глубже из кэша
        deeper = 0
        if children:
            deeper = len(uia_common.cached_children(children[0]))

        print(f"   cache={label:<22} детей {len(children):>4} за {call_ms:>7.1f} мс | "
              f"CachedName читается у {readable}/5 | вглубь из кэша: {deeper} | пример: {sample}")


def test_event_thread() -> None:
    """2. Приходит ли колбэк хука, если поток НЕ разбирает сообщения."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentThreadId.restype = ctypes.c_ulong
    kernel32.GetTickCount64.restype = ctypes.c_ulonglong

    WINPROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                                 ctypes.c_long, ctypes.c_long, wintypes.DWORD, wintypes.DWORD)
    user32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE,
                                       WINPROC, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
    user32.SetWinEventHook.restype = wintypes.HANDLE

    registering_thread = kernel32.GetCurrentThreadId()
    hits: list[tuple[int, float]] = []

    def on_event(hook, event, hwnd, obj, child, thread, when) -> None:
        callback_thread = kernel32.GetCurrentThreadId()
        hits.append((callback_thread, (kernel32.GetTickCount64() & 0xFFFFFFFF) - when))

    callback = WINPROC(on_event)
    hook = user32.SetWinEventHook(0x8002, 0x8002, None, callback, 0, 0, 0x0002)  # EVENT_OBJECT_SHOW

    print(f"[2] поток, зарегистрировавший хук: {registering_thread}")
    # Запускаем окно и НИЧЕГО не откачиваем: только спим. Если аудитор прав —
    # колбэка не будет вовсе; если прав я — придёт на другом потоке.
    import subprocess

    fixture = subprocess.Popen([sys.executable, "-u", str(Path(__file__).resolve().parent / "fixture_window.py"),
                                "--title", "MCP-Hands events thread test", "--click-log",
                                str(Path(__file__).resolve().parent / ".thread_test.log")],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3.0)
    fixture.kill()
    fixture.communicate()
    if hits:
        threads = {thread_id for thread_id, _ in hits}
        print(f"   событий получено: {len(hits)} | потоки колбэка: {threads} | "
              f"задержки: {[round(latency, 1) for _, latency in hits[:5]]}")
        print(f"   вывод: колбэк приходит {'В ДРУГОМ потоке' if threads - {registering_thread} else 'в том же потоке'}")
    else:
        print("   событий не получено: без цикла сообщений колбэк не вызывается")
    user32.UnhookWinEvent(hook)


def test_window_geometry() -> None:
    """3. Расходятся ли GetWindowRect и DWM-границы окна."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
    DWMWA_EXTENDED_FRAME_BOUNDS = 9

    for title in ("Блокнот", "MCP-Hands"):
        windows = [w for w in uia_common.enum_windows() if title in w["title"]]
        if not windows:
            continue
        hwnd = wintypes.HWND(windows[0]["hwnd"])
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        dwm = wintypes.RECT()
        result = dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS,
                                              ctypes.byref(dwm), ctypes.sizeof(dwm))
        win32 = (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
        frame = (dwm.left, dwm.top, dwm.right - dwm.left, dwm.bottom - dwm.top)
        delta = (frame[0] - win32[0], frame[1] - win32[1],
                 (win32[0] + win32[2]) - (frame[0] + frame[2]),
                 (win32[1] + win32[3]) - (frame[1] + frame[3]))
        print(f"[3] {windows[0]['exe']}: GetWindowRect {win32}")
        print(f"    DWM-границы (видимая рамка) {frame} | hr=0x{result & 0xFFFFFFFF:X}")
        print(f"    расхождение по краям (лево, верх, право, низ): {delta}")


if __name__ == "__main__":
    dpi_aware.enable()
    test_cache_scopes(sys.argv[1] if len(sys.argv) > 1 else "opera.exe")
    test_event_thread()
    test_window_geometry()
