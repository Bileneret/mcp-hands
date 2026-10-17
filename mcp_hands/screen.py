"""Захват экрана: DXGI как основной путь и BitBlt как обязательный резерв.

Два факта из замеров, определяющие устройство модуля:

  * DXGI Desktop Duplication отдаёт кадр только при изменении экрана — на статичном
    рабочем столе `grab()` возвращает `None` почти всегда. Поэтому рядом с «свежим
    кадром» всегда есть «последний известный», и агент обязан держать свою копию
    (буфер библиотеки переиспользуется);
  * после сна дисплея DXGI начинает отказывать с `DXGI_ERROR_UNSUPPORTED` и сам не
    восстанавливается. `SetThreadExecutionState(ES_DISPLAY_REQUIRED)` возвращает
    систему в рабочее состояние, но первый кадр после этого стоит ~1.6 с вместо 5 мс.
    Резервный путь через `mss` (BitBlt) обязан быть, иначе агент слепнет ровно тогда,
    когда его позвали после простоя.
"""

from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass, field

import numpy as np

_ES_CONTINUOUS = 0x80000000
_ES_DISPLAY_REQUIRED = 0x00000002
_BLACK_THRESHOLD = 4


class ScreenCaptureError(RuntimeError):
    """Ни один путь захвата не сработал: агенту нечем смотреть на экран."""


@dataclass
class Frame:
    """Кадр в физических пикселях вместе с происхождением — иначе замеры не сравнить.

    `origin` — координаты экрана, которым соответствует левый верхний пиксель кадра.
    У DXGI это первичный монитор, то есть (0, 0); у резервного пути через BitBlt —
    весь виртуальный рабочий стол, который может начинаться с отрицательных значений,
    если монитор стоит слева от основного. Без этого поля кроп окна на таком мониторе
    либо падает, либо возвращает чужую область.
    """

    image: np.ndarray
    source: str
    ms: float
    rect: dict | None = None
    origin: tuple[int, int] = (0, 0)
    suspicious: list[str] = field(default_factory=list)

    @property
    def size(self) -> tuple[int, int]:
        return self.image.shape[1], self.image.shape[0]

    def to_rgb(self):
        """PIL-изображение: конвертация срезом, без OpenCV."""
        from PIL import Image

        return Image.fromarray(self.image[:, :, 2::-1])

    def describe(self) -> dict:
        return {"source": self.source, "ms": round(self.ms, 2), "size": list(self.size),
                "rect": self.rect, "suspicious": self.suspicious}


def keep_display_awake(enable: bool = True) -> None:
    """Запрещает Windows гасить дисплей: на погашенном экране DXGI недоступен."""
    flags = _ES_CONTINUOUS | (_ES_DISPLAY_REQUIRED if enable else 0)
    ctypes.WinDLL("kernel32", use_last_error=True).SetThreadExecutionState(ctypes.c_uint(flags))


class ScreenGrabber:
    """Захват с автоматическим выбором пути и признаками подозрительных кадров."""

    def __init__(self) -> None:
        keep_display_awake(True)  # на погашенном дисплее DXGI недоступен
        self._camera = None
        self._dxgi_error: str | None = None
        self._last: Frame | None = None
        self._last_image: np.ndarray | None = None
        self.log: list[dict] = []

    # --- основной путь ----------------------------------------------------

    def _ensure_camera(self):
        if self._camera is not None:
            return self._camera
        import dxcam

        started = time.perf_counter()
        self._camera = dxcam.create(output_color="BGRA")
        self.log.append({"op": "create_camera", "ms": round((time.perf_counter() - started) * 1000, 2)})
        return self._camera

    def _grab_dxgi(self, timeout_s: float) -> tuple[np.ndarray | None, str]:
        """Возвращает кадр и причину. `None` — не отказ, а «экран не менялся».

        Desktop Duplication отдаёт кадр только при изменении, поэтому на статичном экране
        `grab()` молчит. Если захват ещё не запущен, один раз поднимаем непрерывный режим:
        он даёт первый кадр сразу и делает последующие копии дешёвыми.
        """
        camera = self._ensure_camera()
        frame = camera.grab()
        if frame is not None:
            return frame.copy(), "fresh"
        if not camera.is_capturing:
            camera.start(target_fps=30, video_mode=True)
            deadline = time.perf_counter() + timeout_s
            while time.perf_counter() < deadline:
                latest = camera.get_latest_frame()
                if latest is not None:
                    return latest.copy(), "primed"
                time.sleep(0.01)
            return None, "no-frame"
        latest = camera.get_latest_frame()
        return (latest.copy(), "latest") if latest is not None else (None, "no-frame")

    def _grab_mss(self) -> tuple[np.ndarray, tuple[int, int]]:
        """Резервный путь: BitBlt по всему виртуальному рабочему столу.

        Берётся `monitors[0]` (объединение мониторов), а не `monitors[1]` (первичный):
        кадр обязан покрывать окна на любом мониторе, а начало координат может быть
        отрицательным.
        """
        import mss

        with mss.mss() as session:
            area = session.monitors[0]
            shot = np.array(session.grab(area))
        return shot[:, :, [2, 1, 0, 3]], (area["left"], area["top"])  # BGRA

    # --- публичный интерфейс ---------------------------------------------

    def grab(self, timeout_s: float = 0.25, prefer: str | None = None) -> Frame:
        """Кадр экрана: свежий, при неизменном экране — последний известный.

        Замеры: свежий кадр через DXGI стоит 5–8.5 мс, копия буфера 1.42 мс,
        резервный путь через mss — 33 мс. Ожидание «пока экран изменится» бессмысленно:
        агент работает с тем, что видно сейчас.
        """
        started = time.perf_counter()
        source = "dxgi"
        image = None
        if self._dxgi_error is None:
            try:
                image, reason = self._grab_dxgi(timeout_s)
                if image is None and self._last_image is not None:
                    image = self._last_image
                    source = "dxgi: экран не изменился, использован последний кадр"
                elif image is None:
                    source = f"mss (dxgi не дал первый кадр: {reason})"
            except Exception as exc:  # noqa: BLE001 — храним причину и уходим на резерв
                self._dxgi_error = f"{type(exc).__name__}: {exc}"
        origin = (0, 0)
        if image is None or prefer == "mss":
            try:
                image, origin = self._grab_mss()
                source = "mss (весь виртуальный экран)" if prefer == "mss" \
                    else f"mss (dxgi: {self._dxgi_error})"
            except Exception as exc:  # noqa: BLE001 — оба пути обязаны дать понятный отказ
                self.log.append({"op": "mss_failed", "error": f"{type(exc).__name__}: {exc}"})
                raise ScreenCaptureError(
                    "захват экрана недоступен: DXGI отдал ошибку "
                    f"({self._dxgi_error or 'не использовался'}), резервный BitBlt тоже "
                    f"({type(exc).__name__}: {exc})") from exc
        frame = Frame(image=image, source=source, origin=origin,
                      ms=(time.perf_counter() - started) * 1000.0)
        frame.suspicious = self._check(image)
        self._last, self._last_image = frame, image
        return frame

    @staticmethod
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

    def retry_dxgi(self) -> None:
        """Сбрасывает пометку отказа: вызывается после пробуждения дисплея."""
        if self._camera is not None:
            try:
                self._camera.release()
            except Exception:  # noqa: BLE001
                pass
            self._camera = None
        self._dxgi_error = None

    def crop(self, rect: dict, frame: Frame | None = None) -> Frame:
        """Кроп области экрана из имеющегося кадра: срез numpy, около 0.5 мс.

        Координаты области — экранные; они переводятся в индексы массива через
        начало захваченной области (`frame.origin`). На конфигурации с монитором слева
        от основного это единственный способ получить правильный кроп.
        """
        base = frame or self._last
        if base is None:
            raise RuntimeError("нет кадра: сначала вызовите grab()")
        offset_x, offset_y = base.origin
        height, width = base.image.shape[0], base.image.shape[1]
        left = max(0, min(width, rect["left"] - offset_x))
        top = max(0, min(height, rect["top"] - offset_y))
        right = max(0, min(width, rect["right"] - offset_x))
        bottom = max(0, min(height, rect["bottom"] - offset_y))
        if right <= left or bottom <= top:
            raise ScreenCaptureError(
                f"область {rect} вне захваченного кадра (начало {base.origin}, "
                f"размер {width}x{height}): нужен захват всего виртуального экрана "
                f"(grab(prefer='mss'))")
        started = time.perf_counter()
        patch = base.image[top:bottom, left:right].copy()
        return Frame(image=patch, source=f"{base.source}:crop", origin=(left + offset_x,
                                                                      top + offset_y),
                     ms=(time.perf_counter() - started) * 1000.0,
                     rect={"left": left + offset_x, "top": top + offset_y,
                           "right": right + offset_x, "bottom": bottom + offset_y})

    def _check(self, image: np.ndarray) -> list[str]:
        """Признаки «кадр подозрительный»: чёрный экран или повтор один в один."""
        issues: list[str] = []
        # Сравниваются только каналы цвета: альфа у захвата экрана всегда 255,
        # и по ней чёрный кадр неотличим от нормального.
        if image[:, :, :3].max() <= _BLACK_THRESHOLD:
            issues.append("чёрный кадр")
        if self._last_image is not None and self._last_image.shape == image.shape:
            if np.array_equal(self._last_image, image):
                issues.append("кадр не изменился")
        return issues

    def health(self) -> dict:
        return {"dxgi_error": self._dxgi_error, "has_camera": self._camera is not None,
                "log": self.log[-5:]}
