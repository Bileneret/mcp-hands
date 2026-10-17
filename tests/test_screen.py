"""Живые проверки захвата экрана: кадр, кроп, признаки подозрительного кадра."""

from __future__ import annotations

import pytest

from mcp_hands.geometry import CoordinateSpace
from mcp_hands.screen import Frame, ScreenGrabber, keep_display_awake


@pytest.fixture(scope="module")
def grabber():
    return ScreenGrabber()


@pytest.fixture(autouse=True)
def _needs_capture(require_capture):
    """Без рабочего захвата эти проверки бессмысленны — пропускаем с причиной."""


def test_grab_returns_frame(grabber):
    """Кадр приходит, имеет разумный размер и происходит из известного источника."""
    frame = grabber.grab()
    width, height = frame.size
    assert isinstance(frame, Frame)
    assert width > 100 and height > 100
    assert frame.image.shape[2] == 4, "ожидается BGRA"
    assert frame.source, "источник кадра обязан быть указан для журнала"


def test_grab_is_repeatable_on_static_screen(grabber):
    """Повторный захват на статичном экране не падает и не ждёт «изменения»."""
    first = grabber.grab()
    second = grabber.grab(timeout_s=0.1)
    assert second.size == first.size
    assert second.ms < 200, f"повторный захват не должен ждать таймаут: {second.ms} мс"


def test_crop_uses_window_frame(target):
    """Кроп окна: размер совпадает с видимой рамкой, а не с тенью DWM."""
    space = CoordinateSpace()
    grabber = ScreenGrabber()
    frame = grabber.grab()
    rect = space.window_frame(target.hwnd)
    crop = grabber.crop(rect, frame)
    expected = (rect["right"] - rect["left"], rect["bottom"] - rect["top"])
    assert crop.size == expected
    assert crop.rect is not None and crop.rect["left"] >= 0


def test_crop_rejects_empty_area(grabber):
    """Пустая область — типизированный отказ с объяснением, а не голое ValueError."""
    from mcp_hands.screen import ScreenCaptureError

    grabber.grab()
    with pytest.raises(ScreenCaptureError) as error:
        grabber.crop({"left": 10, "top": 10, "right": 10, "bottom": 10})
    assert "вне захваченного кадра" in str(error.value)


def test_frame_converts_to_rgb_image(grabber):
    """BGRA → PIL без OpenCV: конвертация срезом."""
    frame = grabber.grab()
    image = frame.to_rgb()
    assert image.mode == "RGB"
    assert image.size == frame.size


def test_display_stays_awake():
    """Пробуждение дисплея: вызывается без ошибок и обратимо."""
    keep_display_awake(True)
    keep_display_awake(False)
    keep_display_awake(True)


def test_health_reports_dxgi_state(grabber):
    health = grabber.health()
    assert "dxgi_error" in health
    assert isinstance(health["log"], list)
