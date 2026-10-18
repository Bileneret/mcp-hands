"""Локальный роутер интентов: мгновенные команды без облака («две LLM»: этот
слой — быстрый, старший мозг вызывается только для сложного).

Правило проекта: дешёвое и очевидное решается на месте. «Пауза», «полный экран»,
«открой ютуб», «найди …» — прямые вызовы live-инструментов, без ожидания сети.
Сложные формулировки уходят старшему мозгу (GLM) с каталогом инструментов.
"""

from __future__ import annotations

import re

SITE_ALIASES = {
    "ютуб": "https://www.youtube.com", "youtube": "https://www.youtube.com",
    "гугл": "https://www.google.com", "google": "https://www.google.com",
    "вк": "https://vk.com", "vk": "https://vk.com",
    "телеграм": "https://web.telegram.org", "telegram": "https://web.telegram.org",
    "почт": "https://mail.google.com", "маркет": "https://market.yandex.ru",
}

INTENT_RULES: list[tuple[str, str]] = [
    (r"\b(пауза|останови|стоп видео|приостанови)\b", "video_pause"),
    (r"\b(продолжи|возобнови|играй|включи заново|play)\b", "video_play"),
    (r"(полный экран|весь экран|разверни|фулскрин|fullscreen)", "video_fullscreen"),
    (r"(выйди из полного|сверни видео|обычный экран|выйди из экрана)", "video_exit"),
    (r"(скриншот|снимок экрана|сфотографируй страницу)", "screenshot"),
    (r"(следующ(ее|ий) видео|переключи видео)", "video_next"),
]


def route(text: str) -> dict | None:
    """Возвращает локальный интент {"action", "args", "say"} или None (→ старший мозг)."""
    lowered = text.lower().strip()
    if not lowered:
        return None

    # «открой <сайт> и найди <запрос>» — два шага одной фразой (частая команда)
    combined = re.search(r"\b(открой|открыть|open)\s+([\w\-\.]+).*?\b(найди|поищи|search)\s+(.+)",
                         lowered)
    if combined:
        word, query = combined.group(2).strip(" .,!?"), combined.group(4).strip(" .,!?")
        url = SITE_ALIASES.get(word)
        if url is None and "." in word:
            url = word if word.startswith("http") else f"https://{word}"
        if url and query:
            return {"action": "navigate_search", "args": {"url": url, "query": query},
                    "say": f"Открываю {word} и ищу «{query}»."}

    # «видео по запросу» — составной живой сценарий (раньше простого поиска:
    # «найди видео про новости» должно включить видео, а не искать текст)
    match = re.search(r"\b(видео|ролик|клип)\b.*?\b(про|о|об|под|на тему)\s+(.+)", lowered)
    if match:
        query = match.group(3).strip(" .,!?")
        # хвостовые команды внутри фразы («… и включи на весь экран») — не часть запроса
        query = re.split(r"\s+и\s+(включи|включить|разверни|открой|покажи)\b", query)[0].strip()
        return {"action": "youtube", "args": {"query": query, "fullscreen": True},
                "say": f"Ищу видео: {query}, включаю на весь экран."}
    if "новост" in lowered and any(word in lowered for word in ("включи", "покажи", "видео")):
        return {"action": "youtube", "args": {"query": "новости сегодня",
                                              "fullscreen": True},
                "say": "Включаю свежие новости на весь экран."}

    for pattern, action in INTENT_RULES:
        if re.search(pattern, lowered):
            says = {
                "video_pause": "Пауза.", "video_play": "Продолжаю.",
                "video_fullscreen": "Разворачиваю на весь экран.",
                "video_exit": "Выхожу из полноэкранного режима.",
                "screenshot": "Делаю снимок страницы.",
                "video_next": "Переключаю видео.",
            }
            args = {}
            if action == "video_next":
                # у YouTube «следующее» — Shift+N (доверенная клавиша через CDP)
                args = {"key": "N", "code": "KeyN", "shift": True}
            return {"action": action, "args": args, "say": says.get(action, "")}

    # «найди …» на текущей странице
    match = re.search(r"\b(найди|поищи|search)\s+(.+)", lowered)
    if match:
        query = match.group(2).strip(" .,!?")
        if query:
            return {"action": "search", "args": {"query": query},
                    "say": f"Ищу «{query}»."}

    # «открой <сайт>» — известные алиасы; «открой ютуб и найди X» ловится выше
    match = re.search(r"\b(открой|открыть|open)\s+([\w\-\.]+)", lowered)
    if match:
        word = match.group(2).strip(" .,!?")
        url = SITE_ALIASES.get(word)
        if url is None and "." in word:
            url = word if word.startswith("http") else f"https://{word}"
        if url:
            return {"action": "navigate", "args": {"url": url},
                    "say": f"Открываю {word}."}
    return None
