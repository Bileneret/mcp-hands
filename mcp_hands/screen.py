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


@dataclass
class Frame:
    """Кадр в физических пикселях вместе с происхождением — иначе замеры не сравнить."""

    image: np.ndarray
    source: str
    ms: float
    rect: dict | None = None
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

    def _grab_mss(self) -> np.ndarray:
        import mss

        with mss.mss() as session:
            shot = np.array(session.grab(session.monitors[1]))
        return shot[:, :, [2, 1, 0, 3]]  # BGRA

    # --- публичный интерфейс ---------------------------------------------

    def grab(self, timeout_s: float = 0.25) -> Frame:
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
        if image is None:
            image = self._grab_mss()
            source = f"mss (dxgi: {self._dxgi_error})"
        frame = Frame(image=image, source=source, ms=(time.perf_counter() - started) * 1000.0)
        frame.suspicious = self._check(image)
        self._last, self._last_image = frame, image
        return frame

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
        """Кроп области из имеющегося кадра: срез numpy, около 0.5 мс."""
        base = frame or self._last
        if base is None:
            raise RuntimeError("нет кадра: сначала вызовите grab()")
        left, top = max(0, rect["left"]), max(0, rect["top"])
        right = min(base.image.shape[1], rect["right"])
        bottom = min(base.image.shape[0], rect["bottom"])
        if right <= left or bottom <= top:
            raise ValueError(f"область кропа пуста: {rect}")
        started = time.perf_counter()
        patch = base.image[top:bottom, left:right].copy()
        return Frame(image=patch, source=f"{base.source}:crop",
                     ms=(time.perf_counter() - started) * 1000.0,
                     rect={"left": left, "top": top, "right": right, "bottom": bottom})

    def _check(self, image: np.ndarray) -> list[str]:
        """Признаки «кадр подозрительный»: чёрный экран или повтор один в один."""
        issues: list[str] = []
        if image.max() <= _BLACK_THRESHOLD:
            issues.append("чёрный кадр")
        if self._last_image is not None and self._last_image.shape == image.shape:
            if np.array_equal(self._last_image, image):
                issues.append("кадр не изменился")
        return issues

    def health(self) -> dict:
        return {"dxgi_error": self._dxgi_error, "has_camera": self._camera is not None,
                "log": self.log[-5:]}
