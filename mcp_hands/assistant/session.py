"""Режим разговора Джарвиса: пробуждение, засыпание, стоп-фразы.

Владелец один раз включает ассистента и говорит команду за командой — нажимать
горячую клавишу перед каждой фразой не нужно. Когда пауза затягивается,
ассистент «засыпает» (микрофон больше не слушается), а следующее нажатие
Ctrl+Alt+J будит его снова. Здесь живут только правила режима: без микрофона,
без окон и без голоса — поэтому их видно в тестах.
"""

from __future__ import annotations

import re

#: Фразы, которыми владелец сам заканчивает разговор. Сравнение ТОЧНОЕ: «стоп
#: видео» — команда плееру, а не выход, и не должна глушить разговор.
STOP_PHRASES = frozenset({
    "стоп", "стоп джарвис", "джарвис стоп", "замолчи", "хватит", "спать",
    "отдыхай", "выключись", "пока",
})

DEFAULT_SLEEP_AFTER_S = 60.0
MIN_SLEEP_AFTER_S = 15.0


def normalize(text: str) -> str:
    """Речь → сравнимая строка: нижний регистр, без знаков и двойных пробелов
    («Стоп, Джарвис!» → «стоп джарвис»)."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.lower())).strip()


def is_stop_phrase(text: str) -> bool:
    """Это команда «закончить разговор» (а не «стоп видео» и подобное)?"""
    return normalize(text) in STOP_PHRASES


def sleep_after_seconds(config: dict | None = None,
                        default: float = DEFAULT_SLEEP_AFTER_S,
                        minimum: float = MIN_SLEEP_AFTER_S) -> float:
    """Пауза до засыпания (секунды) из конфига ассистента — с минимумом,
    чтобы опечатка в конфиге не превратила разговор в «мгновенно сплю»."""
    raw = (config or {}).get("sleep_after_s", default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, value)
