"""Пиксельный путь без модели: встроенный OCR Windows (`Windows.Media.Ocr`).

Замер этапа 0: кроп окна 1125×700 распознаётся за 15 мс, движок русского языка в системе
есть, видеопамять не нужна. Это на порядки дешевле локальной VLM (41 с на запрос), поэтому
основой пиксельного пути выбран OCR, а не модель.

Ограничение, зафиксированное тем же замером: на мелком шрифте OCR ошибается
(«gamma» распознал как «Датта»), поэтому его результат нельзя считать истиной —
он лишь подсказка, которую подтверждают UIA или проверка состояния.

Координаты слова возвращаются в пикселях поданного изображения. Если изображение —
кроп окна, к координатам нужно прибавить левый верхний угол окна: за это отвечает
вызывающий (обычно через `geometry.CoordinateSpace`).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

_DEFAULT_LANGUAGE = "ru"


@dataclass
class Word:
    """Слово с распознанным текстом и прямоугольником в пикселях изображения."""

    text: str
    rect: dict
    line_index: int

    @property
    def center(self) -> tuple[int, int]:
        return (int(self.rect["left"] + self.rect["right"]) // 2,
                int(self.rect["top"] + self.rect["bottom"]) // 2)

    def offset(self, dx: int, dy: int) -> "Word":
        """Перенос прямоугольника в координаты экрана."""
        return Word(self.text, {"left": self.rect["left"] + dx, "top": self.rect["top"] + dy,
                                "right": self.rect["right"] + dx, "bottom": self.rect["bottom"] + dy},
                    self.line_index)

    def to_dict(self) -> dict:
        return {"text": self.text, "rect": [self.rect["left"], self.rect["top"],
                                            self.rect["right"], self.rect["bottom"]]}


def available() -> bool:
    """Есть ли в системе движок OCR (на Windows 10/11 он встроен)."""
    try:
        from winsdk.windows.media.ocr import OcrEngine

        return OcrEngine.available_recognizer_languages.size > 0
    except Exception:  # noqa: BLE001 — пакет winsdk может быть не установлен
        return False


def languages() -> list[str]:
    try:
        from winsdk.windows.media.ocr import OcrEngine

        return [language.language_tag for language in OcrEngine.available_recognizer_languages]
    except Exception:  # noqa: BLE001
        return []


class WindowsOcr:
    """Обёртка над `Windows.Media.Ocr` с синхронным интерфейсом."""

    def __init__(self, language: str | None = _DEFAULT_LANGUAGE) -> None:
        self.language = language
        self.last_ms: float | None = None

    def recognize(self, image, offset: tuple[int, int] = (0, 0)) -> list[Word]:
        """Распознаёт изображение (PIL или numpy) и возвращает слова.

        `offset` — левый верхний угол изображения на экране: слова сразу возвращаются
        в координатах экрана, чтобы их можно было сличать с прямоугольниками UIA.
        """
        import io

        from PIL import Image

        if not isinstance(image, Image.Image):
            frame = image
            image = Image.fromarray(frame[:, :, 2::-1] if frame.shape[2] == 4 else frame)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        started = time.perf_counter()
        raw = asyncio.run(self._recognize(buffer.getvalue()))
        self.last_ms = (time.perf_counter() - started) * 1000.0
        return [word.offset(*offset) for word in raw]

    async def _recognize(self, png_bytes: bytes) -> list[Word]:
        from winsdk.windows.globalization import Language
        from winsdk.windows.graphics.imaging import BitmapDecoder
        from winsdk.windows.media.ocr import OcrEngine
        from winsdk.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream)
        writer.write_bytes(png_bytes)
        await writer.store_async()
        await writer.flush_async()
        stream.seek(0)

        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        engine = None
        if self.language:
            engine = OcrEngine.try_create_from_language(Language(self.language))
        engine = engine or OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            raise RuntimeError("OCR недоступен: в системе нет ни одного языкового движка")

        result = await engine.recognize_async(bitmap)
        words: list[Word] = []
        for index, line in enumerate(result.lines):
            for word in line.words:
                box = word.bounding_rect
                words.append(Word(word.text,
                                  {"left": round(box.x), "top": round(box.y),
                                   "right": round(box.x + box.width),
                                   "bottom": round(box.y + box.height)},
                                  index))
        return words

    @staticmethod
    def find(words: list[Word], needle: str, exact: bool = False) -> list[Word]:
        """Поиск слова по тексту: OCR ошибается, поэтому сравнение регистронезависимое."""
        target = needle.lower()
        if exact:
            return [word for word in words if word.text.lower() == target]
        return [word for word in words if target in word.text.lower()]
