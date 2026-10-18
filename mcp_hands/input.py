"""Актуаторы: активация окна, клик в два шага с проверкой курсора, ввод текста.

Правила из замеров и аудита:

  * **Перед кликом окно активируется.** Первый клик по неактивному окну съедается
    активацией: нажатие до элемента не доходит вовсе;
  * **Клик выполняется в два шага**: перемещение → проверка позиции курсора
    (`GetCursorPos`) → нажатие. Пакет MOVE+DOWN+UP на неактивном окне смещает курсор
    (наблюдалось +29 px), а раздельная схема позволяет отменить нажатие, если курсор
    встал не туда;
  * абсолютные координаты помечаются `MOUSEEVENTF_VIRTUALDESK`: без него они проецируются
    только на первичный монитор;
  * **UIPI**: к окну процесса с более высокими правами `SendInput` и `SetForegroundWindow`
    молча не работают, без исключения в Python. Поэтому целостность проверяется заранее
    и отказ сообщается явно.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import time

from .geometry import CoordinateSpace, point_in_rect

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

# Сколько времени активация окна перебирает приёмы передачи фокуса. Бюджет операций
# сателлита обязан быть больше этого значения (см. probe.DOMAIN_OPERATION_TIMEOUT_S).
ACTIVATION_TIMEOUT_S = 1.5
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_WHEEL = 0x0800
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

# Виртуальные клавиши, которые нужны сценариям. Значения — из Windows SDK:
# имена не выдумываются, а берутся по спецификации, потому что «похожие» коды
# на разных раскладках ведут себя по-разному.
VK_BACK = 0x08
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12          # Alt
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_PRIOR = 0x21         # Page Up
VK_NEXT = 0x22          # Page Down
VK_END = 0x23
VK_HOME = 0x24
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_DELETE = 0x2E
VK_LWIN = 0x5B
VK_F4 = 0x73
VK_A = 0x41
VK_C = 0x43
VK_F = 0x46
VK_S = 0x53
VK_V = 0x56
VK_W = 0x57
VK_X = 0x58
VK_Z = 0x5A

TOKEN_QUERY = 0x0008
TOKEN_INTEGRITY_LEVEL = 25
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

INTEGRITY_NAMES = {
    0x0000: "untrusted", 0x1000: "low", 0x2000: "medium", 0x2100: "medium-plus",
    0x3000: "high", 0x4000: "system",
}


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class INPUT(ctypes.Structure):
    class _UNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _UNION)]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", ctypes.c_ulong)]


class TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", SID_AND_ATTRIBUTES)]


def same_text(observed: str, expected: str) -> bool:
    """Сравнивает текст независимо от переводов строк и хвостовых пробелов.

    Приложения отдают разные переводы строк: Блокнот возвращает CR, наши строки
    содержат LF, файлы на диске — CRLF. Без нормализации успешный ввод объявлялся
    неудачей: сценарий трижды перепечатывал текст и падал (найдено на стенде).
    """
    def normalize(value: str) -> str:
        return (value.replace("\r\n", "\n")
                .replace("\r", "\n").strip())

    return normalize(expected) in normalize(observed) if expected.strip() else True


def integrity_level_of_process(pid: int) -> dict:
    """Уровень целостности процесса: от него зависит, дойдёт ли до него ввод.

    Сигнатуры объявлены явно: без них ctypes усекает 64-битные дескрипторы до int
    и следующий же вызов падает с OverflowError вместо ответа.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                             wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
    process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not process:
        return {"level": None, "name": "неизвестно", "rid": None}
    token = wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(process, TOKEN_QUERY, ctypes.byref(token)):
            return {"level": None, "name": "неизвестно", "rid": None}
        size = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, buffer, size.value,
                                            ctypes.byref(size)):
            return {"level": None, "name": "неизвестно", "rid": None}
        label = ctypes.cast(buffer, ctypes.POINTER(TOKEN_MANDATORY_LABEL)).contents
        count = advapi32.GetSidSubAuthorityCount(label.Label.Sid).contents.value
        rid = advapi32.GetSidSubAuthority(label.Label.Sid, count - 1)
        value = int(rid.contents.value)
        return {"level": value, "rid": value,
                "name": INTEGRITY_NAMES.get(value & 0xF000, f"0x{value:X}")}
    finally:
        kernel32.CloseHandle(process)
        if token:
            kernel32.CloseHandle(token)


class Actuator:
    """Ввод в чужие окна: только через этот класс, иначе теряются проверки."""

    def __init__(self, space: CoordinateSpace | None = None) -> None:
        self.space = space or CoordinateSpace()
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._prepare_prototypes()

    def _prepare_prototypes(self) -> None:
        user32 = self.user32
        user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
        user32.SendInput.restype = ctypes.c_uint
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = ctypes.c_bool
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.AttachThreadInput.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_bool]
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(ctypes.c_ulong)]
        user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
        self.kernel32.GetCurrentThreadId.argtypes = []
        self.kernel32.GetCurrentThreadId.restype = ctypes.c_ulong
        self.kernel32.GetCurrentProcessId.argtypes = []
        self.kernel32.GetCurrentProcessId.restype = ctypes.c_ulong
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    # --- низкий уровень ---------------------------------------------------

    def attach_to_input_desktop(self) -> dict:
        """Привязывает поток к рабочему столу ввода.

        Если процесс запущен не в интерактивном рабочем столе (служба, задача планировщика,
        часть окружений IDE), `SendInput` завершается с `ERROR_ACCESS_DENIED`, а
        `GetCursorPos` возвращает (0,0). Переключение потока на рабочий стол ввода
        возвращает обоим функциям работоспособность.

        Вызывать до создания окон и хуков: `SetThreadDesktop` отказывается работать
        у потока, который уже что-то создал.
        """
        user32 = self.user32
        user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        user32.OpenInputDesktop.restype = wintypes.HANDLE
        user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]
        user32.SetThreadDesktop.restype = wintypes.BOOL
        user32.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                     ctypes.c_void_p, wintypes.DWORD,
                                                     ctypes.POINTER(wintypes.DWORD)]
        user32.GetThreadDesktop.argtypes = [wintypes.DWORD]
        user32.GetThreadDesktop.restype = wintypes.HANDLE
        user32.CloseDesktop.argtypes = [wintypes.HANDLE]

        desktop = user32.OpenInputDesktop(0, False, 0x01FF)  # DESKTOP_ALL_ACCESS
        if not desktop:
            self.desktop = {"attached": False, "name": None,
                            "reason": f"OpenInputDesktop: {ctypes.get_last_error()}"}
            return self.desktop
        input_name = self._desktop_name(desktop)
        current = user32.GetThreadDesktop(self.kernel32.GetCurrentThreadId())
        current_name = self._desktop_name(current) if current else None

        # Переключаться нужно только если рабочие столы РАЗНЫЕ. Если поток уже на столе
        # ввода (обычный случай для интерактивной сессии), вызов SetThreadDesktop вернёт
        # ошибку 170 — поток успел создать скрытое окно (их создаёт comtypes для
        # маршалинга COM), и это не признак нерабочего ввода.
        if current_name and current_name == input_name:
            self.desktop = {"attached": True, "name": input_name, "how": "уже на столе ввода",
                            "reason": None}
            return self.desktop
        attached = bool(user32.SetThreadDesktop(desktop))
        self.desktop = {"attached": attached, "name": input_name,
                        "how": "переключён" if attached else "не удалось",
                        "thread_desktop": current_name,
                        "reason": None if attached else
                                  f"SetThreadDesktop: {ctypes.get_last_error()} "
                                  f"(170 — у потока уже есть окна или хуки)"}
        return self.desktop

    def own_desktop(self) -> str:
        """Имя рабочего стола текущего потока."""
        desktop = self.user32.GetThreadDesktop(self.kernel32.GetCurrentThreadId())
        return self._desktop_name(desktop) if desktop else ""

    def window_desktop(self, hwnd: int) -> str:
        """Имя рабочего стола, на котором создано окно.

        Ввод уходит на рабочий стол вызывающего потока. Окно параллельного рабочего стола
        (служба, песочница, заблокированный сеанс) физически не получит ни мышь, ни
        клавиатуру — и это надо называть вслух, а не кликать в пустоту.
        """
        thread_id = self.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), None)
        desktop = self.user32.GetThreadDesktop(thread_id) if thread_id else 0
        return self._desktop_name(desktop) if desktop else ""

    @staticmethod
    def _desktop_name(handle) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        needed = ctypes.wintypes.DWORD(0)
        ok = ctypes.WinDLL("user32", use_last_error=True).GetUserObjectInformationW(
            handle, 2, buffer, ctypes.sizeof(buffer), ctypes.byref(needed))  # UIO_NAME
        return buffer.value if ok else f"<ошибка {ctypes.get_last_error()}>"

    def _send(self, *spec) -> int:
        """Отправляет события ввода.

        Спецификация события — `(вид, флаги, x, y, значение)`. Для клавиатуры значение
        означает разное в зависимости от флага: при `KEYEVENTF_UNICODE` это код символа
        (`wScan`), иначе — виртуальная клавиша (`wVk`). Без этого разделения Windows
        игнорирует событие: `wVk` остаётся нулевым, а `wScan` без флага `KEYEVENTF_SCANCODE`
        не читается вовсе, и хоткей не срабатывает.
        """
        events = (INPUT * len(spec))()
        for index, (kind, flags, x, y, value) in enumerate(spec):
            events[index].type = kind
            if kind == INPUT_MOUSE:
                if flags & MOUSEEVENTF_ABSOLUTE:
                    flags |= MOUSEEVENTF_VIRTUALDESK
                # Величина прокрутки колеса живёт в mouseData: без неё событие
                # уходит «нулевым» и содержимое не двигается (найдено живой приёмкой).
                wheel = value if flags & MOUSEEVENTF_WHEEL else 0
                events[index].mi = MOUSEINPUT(x, y, wheel, flags, 0, None)
            elif flags & KEYEVENTF_UNICODE:
                events[index].ki = KEYBDINPUT(0, value, flags, 0, None)
            else:
                events[index].ki = KEYBDINPUT(value, 0, flags, 0, None)
        return self.user32.SendInput(len(spec), events, ctypes.sizeof(INPUT))

    def cursor(self) -> tuple[int, int]:
        point = wintypes.POINT()
        self.user32.GetCursorPos(ctypes.byref(point))
        return point.x, point.y

    def foreground(self) -> int:
        return int(self.user32.GetForegroundWindow() or 0)

    def window_alive(self, hwnd: int) -> bool:
        """Дескриптор и окно существует: проверка dry_run без побочных эффектов."""
        return bool(self.user32.IsWindow(hwnd))

    def foreground_info(self) -> dict:
        """Что сейчас на переднем плане: нужно для внятного отказа."""
        hwnd = self.foreground()
        if not hwnd:
            return {"hwnd": 0, "title": "", "exe": ""}
        buffer = ctypes.create_unicode_buffer(256)
        self.user32.GetWindowTextW(hwnd, buffer, 256)
        pid = ctypes.wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        return {"hwnd": hwnd, "title": buffer.value, "pid": pid.value}

    def guard_keyboard(self, expect_hwnd: int, allow_any_window: bool = False) -> dict:
        """Проверяет, что ввод уйдёт именно в ожидаемое окно.

        Клавиши действуют на то окно, которое активно ПРЯМО СЕЙЧАС. Если это не наша
        цель, нажатие уходит в чужое приложение — так `Ctrl+W`, отправленный ради
        закрытия своей вкладки, закрывал вкладки браузера и чужие программы. Поэтому:

          * обычный путь — указать `expect_hwnd`, и ввод выполняется только при совпадении;
          * без `expect_hwnd` ввод запрещён; осознанный обход — `allow_any_window=True`.
        """
        if allow_any_window:
            return {"ok": True, "how": "явно разрешено любое окно"}
        if not expect_hwnd:
            return {"ok": False, "reason": "не указано ожидаемое окно: ввод отменён, "
                                           "чтобы нажатие не ушло в чужое приложение "
                                           "(обход: allow_any_window=True)"}
        current = self.foreground()
        if current != expect_hwnd:
            return {"ok": False, "expected": expect_hwnd,
                    "reason": f"активно не наше окно ({hex(current)} "
                              f"{self.foreground_info()['title'][:40]!r}) — нажатие отменено"}
        return {"ok": True, "how": "наше окно активно"}

    # --- активация --------------------------------------------------------

    def can_control(self, pid: int) -> dict:
        """Дойдёт ли ввод до процесса: сравниваем уровни целостности (UIPI)."""
        ours = integrity_level_of_process(self.kernel32.GetCurrentProcessId())
        theirs = integrity_level_of_process(pid)
        blocked = (ours["level"] is not None and theirs["level"] is not None
                   and theirs["level"] > ours["level"])
        return {"ours": ours, "target": theirs, "blocked_by_uipi": blocked}

    def activate(self, hwnd: int, timeout_s: float = ACTIVATION_TIMEOUT_S) -> dict:
        """Делает окно активным. Прямой вызов часто отклоняется Windows, поэтому три приёма."""
        handle = wintypes.HWND(hwnd)
        if self.foreground() == hwnd:
            return {"ok": True, "how": "already"}
        self.user32.ShowWindow(handle, 9)  # SW_RESTORE
        deadline = time.perf_counter() + timeout_s
        while time.perf_counter() < deadline:
            current = self.kernel32.GetCurrentThreadId()
            foreground = self.foreground()
            other = self.user32.GetWindowThreadProcessId(wintypes.HWND(foreground), None) if foreground else 0
            attached = bool(other) and other != current
            if attached:
                self.user32.AttachThreadInput(current, other, True)
            try:
                self.user32.SetForegroundWindow(handle)
                if self.foreground() == hwnd:
                    return {"ok": True, "how": "attached" if attached else "direct"}
            finally:
                if attached:
                    self.user32.AttachThreadInput(current, other, False)
            self._tap_alt()  # снимает блокировку передачи фокуса
            time.sleep(0.15)
        return {"ok": self.foreground() == hwnd, "how": "timeout"}

    def _tap_alt(self) -> None:
        self._send((INPUT_KEYBOARD, 0, 0, 0, VK_MENU),
                   (INPUT_KEYBOARD, KEYEVENTF_KEYUP, 0, 0, VK_MENU))

    # --- клик -------------------------------------------------------------

    def move(self, x: int, y: int) -> tuple[int, int]:
        self._send((INPUT_MOUSE, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, *self.space.to_normalized(x, y), 0))

    WM_MOUSEWHEEL = 0x020A
    WHEEL_DELTA = 120

    def scroll_message(self, hwnd: int, clicks: int, x: int, y: int) -> dict:
        """Прокрутка СООБЩЕНИЕМ в окно: курсор пользователя не трогается вовсе.

        Это предпочтительный путь (фоновая доставка ввода: человек может работать
        мышью параллельно). Часть приложений, однако, реагирует на настоящее колесо —
        для них есть `scroll_wheel`; выбор делает вызывающий по факту кадра.
        """
        self.user32.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                             wintypes.WPARAM, wintypes.LPARAM]
        self.user32.PostMessageW.restype = ctypes.c_bool
        delta = self.WHEEL_DELTA * (1 if clicks > 0 else -1) * abs(clicks)
        lparam = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
        wparam = (delta & 0xFFFF) << 16
        for _ in range(abs(clicks)):
            ok = self.user32.PostMessageW(int(hwnd), self.WM_MOUSEWHEEL, wparam, lparam)
            if not ok:
                return {"ok": False, "how": "post_message", "clicks": clicks,
                        "reason": "окно не приняло сообщение прокрутки"}
        return {"ok": True, "how": "post_message", "clicks": clicks,
                "point": [int(x), int(y)]}

    def scroll_wheel(self, clicks: int, expect_hwnd: int = 0, point: tuple[int, int] | None = None,
                     restore_cursor: bool = True) -> dict:
        """Настоящее колесо в указанной точке (курсор вернётся на место, если просили).

        Используется как резерв, когда сообщение окно проигнорировало: колесо —
        физическое событие, его видят и Chromium, и игры. Перемещение курсора
        восстанавливается, чтобы не мешать владельцу.
        """
        if expect_hwnd:
            guard = self.guard_keyboard(expect_hwnd)
            if not guard.get("ok"):
                return {"ok": False, "how": "send_input", "reason": guard.get("reason", "")}
        before = self.cursor() if restore_cursor else None
        target = point or before or (0, 0)
        self.move(int(target[0]), int(target[1]))
        time.sleep(0.05)
        delta = self.WHEEL_DELTA * (1 if clicks > 0 else -1) * abs(clicks)
        events = self._send((INPUT_MOUSE, MOUSEEVENTF_WHEEL | MOUSEEVENTF_ABSOLUTE,
                             *self.space.to_normalized(int(target[0]), int(target[1])),
                             delta & 0xFFFFFFFF),)
        if restore_cursor and before:
            time.sleep(0.05)
            self.move(int(before[0]), int(before[1]))
        return {"ok": bool(events), "how": "send_input", "clicks": clicks,
                "point": [int(target[0]), int(target[1])], "restored": bool(before)}


    def click(self, x: int, y: int, verify: bool = True, tolerance: int = 4,
              settle_s: float = 0.05, allow_unverified: bool = False) -> dict:
        """Перемещение, проверка позиции курсора, нажатие.

        Нажатие отправляется С абсолютными координатами цели: даже если перемещение
        не отработало, система поставит курсор в нужную точку в момент щелчка.
        Иначе нажатие ушло бы в текущую позицию курсора, где может оказаться
        что угодно — например, кнопка закрытия чужого окна.

        Если позицию курсора подтвердить нельзя (неинтерактивный рабочий стол),
        нажатие блокируется: слепой клик в неизвестное место недопустим. Осознанный
        обход — только явным аргументом `allow_unverified=True`.
        """
        self.move(x, y)
        if settle_s:
            time.sleep(settle_s)
        landed = self.cursor()
        cursor_available = not (landed == (0, 0) and (x, y) != (0, 0))
        if not cursor_available:
            self.move(x, y)  # повторная попытка: возможно, ввод ещё не обработан
            time.sleep(settle_s)
            landed = self.cursor()
            cursor_available = not (landed == (0, 0) and (x, y) != (0, 0))
        if verify and not cursor_available and not allow_unverified:
            return {"ok": False, "target": [x, y], "landed": list(landed),
                    "cursor_check": "недоступен", "events": 0,
                    "reason": "позиция курсора не подтверждена — слепой клик заблокирован "
                              "(обход: allow_unverified=True)"}
        if verify and cursor_available and (
                abs(landed[0] - x) > tolerance or abs(landed[1] - y) > tolerance):
            return {"ok": False, "target": [x, y], "landed": list(landed),
                    "cursor_check": "промах", "events": 0,
                    "reason": "курсор не встал на цель — нажатие отменено"}
        normalized = self.space.to_normalized(x, y)
        # У нажатия и отпускания флаги СВОИ: событие с одновременными LEFTDOWN и LEFTUP
        # система трактует непредсказуемо, а координаты цели нужны обоим.
        sent = self._send((INPUT_MOUSE, MOUSEEVENTF_LEFTDOWN | MOUSEEVENTF_ABSOLUTE, *normalized, 0),
                          (INPUT_MOUSE, MOUSEEVENTF_LEFTUP | MOUSEEVENTF_ABSOLUTE, *normalized, 0))
        result = {"ok": sent == 2, "target": [x, y], "landed": list(landed), "events": sent,
                  "cursor_check": "проверен" if cursor_available else "недоступен"}
        if sent != 2:
            result["reason"] = (f"SendInput отправил {sent} из 2 событий: "
                                f"WinError {ctypes.get_last_error()} "
                                f"(5 — доступ запрещён: изоляция рабочего стола или UIPI)")
        return result

    def click_node(self, node: dict, pid: int = 0, activate: bool = True,
                   window_hwnd: int = 0) -> dict:
        """Клик по элементу: активация окна, проверка UIPI, клик.

        Окно поднимается ВСЕГДА, когда известен его дескриптор: элемент из UIA не несёт
        `hwnd` (это свойство окна, а не кнопки), и без явной активации клик уходит
        в перекрывающее окно — например, в консоль поверх мишени.
        """
        rect = node.get("rect")
        if not rect:
            return {"ok": False, "reason": "у элемента нет прямоугольника"}
        center = node.get("center") or [(rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2]
        result = {"node": {"name": node.get("name"), "control_type": node.get("control_type"),
                           "rect": rect}, "center": center}
        if pid:
            rights = self.can_control(pid)
            result["rights"] = rights
            if rights["blocked_by_uipi"]:
                return {**result, "ok": False,
                        "reason": "целевое окно запущено с более высокими правами: ввод не дойдёт"}
        target_hwnd = window_hwnd or node.get("hwnd") or 0
        if target_hwnd:
            ours, theirs = self.own_desktop(), self.window_desktop(target_hwnd)
            result["desktop"] = {"ours": ours, "target": theirs}
            if ours and theirs and not ours.startswith("<") and not theirs.startswith("<") \
                    and ours != theirs:
                return {**result, "ok": False,
                        "reason": f"окно цели на рабочем столе {theirs!r}, ввод идёт "
                                  f"на {ours!r}: клик туда не дойдёт"}
        if activate and target_hwnd:
            result["activation"] = self.activate(target_hwnd)
        result.update(self.click(*center))
        return result

    # --- клавиатура -------------------------------------------------------

    def type_text(self, text: str, char_delay_s: float = 0.012, expect_hwnd: int = 0,
                  allow_any_window: bool = False) -> dict:
        """Ввод текста через SendInput в режиме Unicode: кириллица и символы вне BMP.

        Строка переводится в 16-битные code units UTF-16: символы вне основной плоскости
        (эмодзи и прочие) отправляются суррогатной парой, как требует Windows. Прямая
        передача `ord(char)` в 16-битное поле не падает, а **молча обрезает** код
        (`0x1F680` превращалось в `0xF680`), вводя неверный символ.

        Пауза между символами по умолчанию ненулевая: современные приложения (WinUI-Блокнот)
        теряют часть символов при плотном потоке событий — замерено, что из 30 знаков
        доходило около половины. 12 мс на символ незаметны для задачи и убирают потери.
        """
        guard = self.guard_keyboard(expect_hwnd, allow_any_window)
        if not guard["ok"]:
            return {"ok": False, "chars": 0, "reason": guard["reason"]}

        units: list[int] = []
        for char in text:
            encoded = char.encode("utf-16-le")
            units.extend(int.from_bytes(encoded[index:index + 2], "little")
                         for index in range(0, len(encoded), 2))
        returns = 0
        for unit in units:
            if unit == 0x0A:  # перевод строки
                # Юникод-символ перевода строки современные поля игнорируют: WinUI-Блокнот
                # склеивал строки. Нажимаем настоящую клавишу Enter.
                self._send((INPUT_KEYBOARD, 0, 0, 0, VK_RETURN),
                           (INPUT_KEYBOARD, KEYEVENTF_KEYUP, 0, 0, VK_RETURN))
                returns += 1
            else:
                self._send((INPUT_KEYBOARD, KEYEVENTF_UNICODE, 0, 0, unit),
                           (INPUT_KEYBOARD, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0, unit))
            if char_delay_s:
                time.sleep(char_delay_s)
        return {"ok": True, "chars": len(text), "code_units": len(units), "returns": returns}

    def type_text_verified(self, text: str, read_back, attempts: int = 3,
                           char_delay_s: float = 0.012, settle_s: float = 0.4,
                           expect_hwnd: int = 0, allow_any_window: bool = False) -> dict:
        """Вводит текст и ПРОВЕРЯЕТ, что он дошёл; при потере — очищает и повторяет.

        Постусловие важнее вызова: приложение может потерять часть символов (WinUI-Блокнот
        терял половину при плотном потоке), и без проверки агент считал бы шаг успешным.
        `read_back` — функция без аргументов, возвращающая текущий текст поля.
        """
        last = ""
        for attempt in range(1, max(1, attempts) + 1):
            typing = self.type_text(text, char_delay_s=char_delay_s, expect_hwnd=expect_hwnd,
                                    allow_any_window=allow_any_window)
            if not typing.get("ok"):
                return {"ok": False, "attempts": attempt, "reason": typing.get("reason", ""),
                        "observed": last[:120]}
            time.sleep(settle_s)
            last = read_back() or ""
            if same_text(last, text):
                return {"ok": True, "attempts": attempt, "chars": len(text),
                        "observed": last[:120]}
            # Очищаем поле и пробуем снова: Ctrl+A, затем Delete.
            self.hotkey(VK_CONTROL, VK_A, expect_hwnd=expect_hwnd,
                        allow_any_window=allow_any_window)
            time.sleep(0.1)
            self._send((INPUT_KEYBOARD, 0, 0, 0, VK_DELETE),
                       (INPUT_KEYBOARD, KEYEVENTF_KEYUP, 0, 0, VK_DELETE))
            time.sleep(0.2)
        return {"ok": False, "attempts": attempts, "chars": len(text),
                "observed": last[:120],
                "reason": "текст не дошёл до поля целиком"}

    def hotkey(self, *virtual_keys: int, expect_hwnd: int = 0,
               allow_any_window: bool = False) -> dict:
        guard = self.guard_keyboard(expect_hwnd, allow_any_window)
        if not guard["ok"]:
            return {"ok": False, "keys": list(virtual_keys), "reason": guard["reason"]}
        for key in virtual_keys:
            self._send((INPUT_KEYBOARD, 0, 0, 0, key))
        for key in reversed(virtual_keys):
            self._send((INPUT_KEYBOARD, KEYEVENTF_KEYUP, 0, 0, key))
        return {"ok": True, "keys": list(virtual_keys)}

    def click_inside(self, rect: dict, x: int, y: int) -> bool:
        """Проверка, что точка внутри прямоугольника — защита от промаха по координатам."""
        return point_in_rect(x, y, rect)
