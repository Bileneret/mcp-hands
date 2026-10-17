"""Проверки пиксельного пути: OCR находит текст элемента и попадает в его прямоугольник.

Тест связывает три слоя сразу: захват кадра, OCR и координаты UIA. Если они расходятся
(например, из-за тени DWM или масштаба DPI), слово окажется вне прямоугольника кнопки —
и это будет видно как провал проверки, а не как «иногда не туда кликает».
"""

from __future__ import annotations

import pytest

from mcp_hands.geometry import CoordinateSpace
from mcp_hands.ocr import WindowsOcr, available, languages
from mcp_hands.screen import ScreenGrabber
from mcp_hands.uia import find_window


pytestmark = pytest.mark.skipif(not available(), reason="в системе нет движка Windows OCR")


@pytest.fixture(autouse=True)
def _needs_capture(require_capture):
    """OCR проверяется на живом кропе окна, значит нужен рабочий захват."""


def test_ocr_reports_available_languages():
    tags = languages()
    assert tags, "список языков пуст"
    assert any(tag.startswith("ru") for tag in tags), f"нет русского движка: {tags}"


def test_ocr_finds_button_label_inside_its_rect(target):
    """Слово, распознанное на кропе окна, обязано попасть в прямоугольник кнопки из UIA."""
    space = CoordinateSpace()
    grabber = ScreenGrabber()
    frame = grabber.grab()
    rect = space.window_frame(target.hwnd)
    crop = grabber.crop(rect, frame)

    ocr = WindowsOcr()
    words = ocr.recognize(crop.image, offset=(rect["left"], rect["top"]))
    assert words, "OCR не нашёл ни одного слова на кропе окна"
    assert ocr.last_ms is not None and ocr.last_ms < 200, \
        f"OCR неожиданно медленный: {ocr.last_ms:.0f} мс"

    window = find_window(title_contains="pytest target")
    from mcp_hands.uia import UiaClient

    client = UiaClient()
    for name in ("alpha", "beta"):
        node = client.find(client.content_root(window["hwnd"])[0], name=name, control_type="button")
        assert node is not None, f"элемент {name} не найден через UIA"
        found = WindowsOcr.find(words, name)
        assert found, f"OCR не увидел подпись {name!r}: {[word.text for word in words]}"
        word = found[0]
        box = node["rect"]
        assert box[0] - 10 <= word.center[0] <= box[2] + 10, \
            f"слово {word.text!r} вне кнопки по X: {word.center} против {box}"


def test_ocr_reports_measured_time(target):
    """Время распознавания измеряется и попадает в метрику шага."""
    space = CoordinateSpace()
    grabber = ScreenGrabber()
    rect = space.window_frame(target.hwnd)
    crop = grabber.crop(rect, grabber.grab())
    ocr = WindowsOcr()
    ocr.recognize(crop.image)
    assert ocr.last_ms is not None and 0 < ocr.last_ms < 500


def test_ocr_find_is_tolerant():
    """Сравнение текста регистронезависимое и допускает частичное совпадение."""
    from mcp_hands.ocr import Word

    words = [Word("Alpha", {"left": 0, "top": 0, "right": 10, "bottom": 10}, 0),
             Word("beta", {"left": 20, "top": 0, "right": 30, "bottom": 10}, 1)]
    assert WindowsOcr.find(words, "alpha")
    assert WindowsOcr.find(words, "BET")
    assert not WindowsOcr.find(words, "gamma")
    assert WindowsOcr.find(words, "alpha", exact=True)
    assert not WindowsOcr.find(words, "alph", exact=True)
