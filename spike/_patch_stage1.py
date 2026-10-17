"""Патч по итогам аудита этапа 1: применяется один раз, оставлен как след правок.

Правки подтверждены воспроизведением (`spike/reproduce_audit_stage1.py`).
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def patch(relative: str, replacements: list[tuple[str, str]], note: str) -> None:
    path = ROOT / relative
    text = path.read_text(encoding="utf-8")
    for old, new in replacements:
        if old not in text:
            raise SystemExit(f"не найден фрагмент в {relative}: {old[:60]!r}")
        text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")
    print(f"{relative}: {note}")


patch("mcp_hands/input.py", [
    ('''    def _send(self, *spec) -> int:
        events = (INPUT * len(spec))()
        for index, (kind, flags, x, y, scan) in enumerate(spec):
            events[index].type = kind
            if kind == INPUT_MOUSE:
                if flags & MOUSEEVENTF_ABSOLUTE:
                    flags |= MOUSEEVENTF_VIRTUALDESK
                events[index].mi = MOUSEINPUT(x, y, 0, flags, 0, None)
            else:
                events[index].ki = KEYBDINPUT(0, scan, flags, 0, None)
        return self.user32.SendInput(len(spec), events, ctypes.sizeof(INPUT))''',
     '''    def _send(self, *spec) -> int:
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
                events[index].mi = MOUSEINPUT(x, y, 0, flags, 0, None)
            elif flags & KEYEVENTF_UNICODE:
                events[index].ki = KEYBDINPUT(0, value, flags, 0, None)
            else:
                events[index].ki = KEYBDINPUT(value, 0, flags, 0, None)
        return self.user32.SendInput(len(spec), events, ctypes.sizeof(INPUT))'''),
    ('''    def click(self, x: int, y: int, verify: bool = True, tolerance: int = 4,
              settle_s: float = 0.05) -> dict:
        """Перемещение, проверка позиции курсора, нажатие."""
        self.move(x, y)
        if settle_s:
            time.sleep(settle_s)
        landed = self.cursor()
        if verify and (abs(landed[0] - x) > tolerance or abs(landed[1] - y) > tolerance):
            return {"ok": False, "target": [x, y], "landed": list(landed),
                    "reason": "курсор не встал на цель — нажатие отменено"}
        sent = self._send((INPUT_MOUSE, MOUSEEVENTF_LEFTDOWN, 0, 0, 0),
                          (INPUT_MOUSE, MOUSEEVENTF_LEFTUP, 0, 0, 0))
        return {"ok": sent == 2, "target": [x, y], "landed": list(landed), "events": sent}''',
     '''    def click(self, x: int, y: int, verify: bool = True, tolerance: int = 4,
              settle_s: float = 0.05) -> dict:
        """Перемещение, проверка позиции курсора, нажатие.

        Проверка курсора — страховка от промаха при смешении координатных пространств,
        но она не должна блокировать ввод там, где система вообще не отдаёт позицию:
        `GetCursorPos` возвращает (0,0) в неинтерактивной сессии. Тогда нажатие
        выполняется, а результат помечается `cursor_check: "недоступен"`.
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
        if verify and cursor_available and (
                abs(landed[0] - x) > tolerance or abs(landed[1] - y) > tolerance):
            return {"ok": False, "target": [x, y], "landed": list(landed),
                    "cursor_check": "промах",
                    "reason": "курсор не встал на цель — нажатие отменено"}
        sent = self._send((INPUT_MOUSE, MOUSEEVENTF_LEFTDOWN, 0, 0, 0),
                          (INPUT_MOUSE, MOUSEEVENTF_LEFTUP, 0, 0, 0))
        return {"ok": sent == 2, "target": [x, y], "landed": list(landed), "events": sent,
                "cursor_check": "проверен" if cursor_available else "недоступен"}'''),
    ('''    def type_text(self, text: str, char_delay_s: float = 0.0) -> dict:
        """Ввод текста через SendInput в режиме Unicode: работает с кириллицей."""
        for char in text:
            code = ord(char)
            self._send((INPUT_KEYBOARD, KEYEVENTF_UNICODE, 0, 0, code),
                       (INPUT_KEYBOARD, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0, code))
            if char_delay_s:
                time.sleep(char_delay_s)
        return {"ok": True, "chars": len(text)}''',
     '''    def type_text(self, text: str, char_delay_s: float = 0.0) -> dict:
        """Ввод текста через SendInput в режиме Unicode: кириллица и символы вне BMP.

        Строка переводится в 16-битные code units UTF-16: символы вне основной плоскости
        (эмодзи и прочие) отправляются суррогатной парой, как требует Windows. Прямая
        передача `ord(char)` в 16-битное поле не падает, а **молча обрезает** код
        (`0x1F680` превращалось в `0xF680`), вводя неверный символ.
        """
        units: list[int] = []
        for char in text:
            encoded = char.encode("utf-16-le")
            units.extend(int.from_bytes(encoded[index:index + 2], "little")
                         for index in range(0, len(encoded), 2))
        for unit in units:
            self._send((INPUT_KEYBOARD, KEYEVENTF_UNICODE, 0, 0, unit),
                       (INPUT_KEYBOARD, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0, unit))
            if char_delay_s:
                time.sleep(char_delay_s)
        return {"ok": True, "chars": len(text), "code_units": len(units)}'''),
], "хоткеи, ввод Unicode, поведение при недоступном курсоре")

patch("mcp_hands/geometry.py", [
    ('''        area = rect or self.virtual_rect
        width = max(1, area["right"] - area["left"] - 1)
        height = max(1, area["bottom"] - area["top"] - 1)
        return (round((x - area["left"]) * 65535 / width),
                round((y - area["top"]) * 65535 / height))''',
     '''        area = rect or self.virtual_rect
        width = max(1, area["right"] - area["left"] - 1)
        height = max(1, area["bottom"] - area["top"] - 1)
        # Ограничение обязательно: значение больше 65535 переполняет 16-битный регистр
        # мыши в SendInput, и курсор улетает непредсказуемо.
        return (max(0, min(65535, round((x - area["left"]) * 65535 / width))),
                max(0, min(65535, round((y - area["top"]) * 65535 / height))))'''),
], "ограничение диапазона нормализации")

patch("mcp_hands/events.py", [
    ('''FILE_NOTIFY_CHANGE_FILE_NAME = 0x00000001''',
     '''FILE_NOTIFY_CHANGE_FILE_NAME = 0x00000001
FILE_NOTIFY_CHANGE_SIZE = 0x00000008
FILE_NOTIFY_CHANGE_LAST_WRITE = 0x00000010'''),
    ('''            if kernel32.ReadDirectoryChangesW(self._handle, buffer, ctypes.sizeof(buffer), False,
                                              FILE_NOTIFY_CHANGE_FILE_NAME, ctypes.byref(returned),
                                              None, None) and returned.value:''',
     '''            # Одного FILE_NOTIFY_CHANGE_FILE_NAME мало: он срабатывает на создание,
            # удаление и переименование, но не на перезапись содержимого. Сохранение
            # отчёта поверх существующего файла приходит как изменение размера и записи.
            if kernel32.ReadDirectoryChangesW(self._handle, buffer, ctypes.sizeof(buffer), False,
                                              FILE_NOTIFY_CHANGE_FILE_NAME
                                              | FILE_NOTIFY_CHANGE_SIZE
                                              | FILE_NOTIFY_CHANGE_LAST_WRITE,
                                              ctypes.byref(returned), None, None) and returned.value:'''),
], "наблюдение за перезаписью файлов")
