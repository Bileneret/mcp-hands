"""Патч №3: автоподбор захвата под монитор окна и проверка мультимониторного кропа."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def patch(relative: str, replacements: list[tuple[str, str]], note: str) -> None:
    path = ROOT / relative
    text = path.read_text(encoding="utf-8")
    for old, new in replacements:
        if old not in text:
            raise SystemExit(f"не найден фрагмент в {relative}: {old[:70]!r}")
        text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")
    print(f"{relative}: {note}")


patch("mcp_hands/screen.py", [
    ('''    def retry_dxgi(self) -> None:''',
     '''    @staticmethod
    def covers(frame: Frame, rect: dict) -> bool:
        """Полностью ли попадает область в кадр: проверка перед кропом."""
        offset_x, offset_y = frame.origin
        height, width = frame.image.shape[0], frame.image.shape[1]
        return (rect["left"] >= offset_x and rect["top"] >= offset_y
                and rect["right"] <= offset_x + width and rect["bottom"] <= offset_y + height)

    def crop_window(self, space, hwnd: int, frame: Frame | None = None) -> Frame:
        """Кроп окна с автоподбором пути захвата под его монитор.

        DXGI в текущей обвязке отдаёт первичный монитор, поэтому окно на мониторе слева
        или справа в его кадр не попадает. Тогда берётся резервный путь: он снимает весь
        виртуальный рабочий стол вместе с отрицательными координатами.
        """
        rect = space.window_frame(hwnd)
        base = frame or self._last
        if base is None or not self.covers(base, rect):
            base = self.grab(prefer="mss")
            if not self.covers(base, rect):
                raise ScreenCaptureError(
                    f"окно {rect} не попало даже в кадр всего виртуального экрана "
                    f"(начало {base.origin}, размер {base.size})")
        return self.crop(rect, base)

    def retry_dxgi(self) -> None:'''),
], "автоподбор захвата под монитор окна")

patch("mcp_hands/geometry.py", [
    ('''    def window_frame(self, hwnd: int) -> dict:''',
     '''    def window_rect_raw(self, hwnd: int) -> dict:
        """Прямоугольник из `GetWindowRect`: включает невидимые поля тени DWM.

        Оставлен для диагностики: разницу с видимой рамкой полезно видеть в журнале.
        """
        raw = wintypes.RECT()
        if not self.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(raw)):
            raise OSError(f"GetWindowRect failed for hwnd={hwnd}")
        return {"left": raw.left, "top": raw.top, "right": raw.right, "bottom": raw.bottom}

    def window_frame(self, hwnd: int) -> dict:'''),
], "доступ к «сырому» прямоугольнику окна для диагностики")
