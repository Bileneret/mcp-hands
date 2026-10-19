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

#: «Видишь ли X на экране?» — вопрос к глазам, а не к памяти и не к странице.
#: Ключ — как называет владелец, значение — что искать в распознанном тексте
#: (OCR видит подпись плитки «YouTube», а не русское «ютуб»).
VISION_VERBS = r"(видишь|видно|видите|увидишь|на экране|посмотри|глянь|погляди|есть ли)"
SCREEN_WORDS = {
    "ютуб": "youtube", "youtube": "youtube",
    "твич": "twitch", "twitch": "twitch",
    "дискорд": "discord", "discord": "discord",
    "опера": "opera", "оперу": "opera", "браузер": "opera",
    "блокнот": "блокнот", "гугл": "google", "google": "google",
    "телеграм": "telegram", "telegram": "telegram",
}


def _site_in(text: str) -> str | None:
    """Какой сайт назван в фразе. Названия матчатся как слова с падежным хвостом
    («ютубе», «оперу»), поэтому короткий алиас «вк» не ловится внутри других
    слов («вконце»). Шаблон собирается конкатенацией: в f-строке `{0,2}`
    превратилось бы в выражение-кортеж, а не в квантификатор."""
    for alias in SITE_ALIASES:
        if re.search(r"\b" + re.escape(alias) + r"[а-яё]{0,2}\b", text):
            return alias
    return None


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
    # «включи свежую новостную сводку МАЙКЛА НАКИ» — это поиск конкретного ролика,
    # а не «включи новости»: локальные правила решают только короткие команды, иначе
    # они открывали первые попавшиеся новости вместо нужного канала (живой прогон 19.10).
    semantic_media = any(word in lowered
                         for word in ("видео", "ролик", "клип", "сводк", "канал"))
    if ("новост" in lowered and any(word in lowered for word in ("включи", "покажи", "видео"))
            and not semantic_media):
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

    # «Видишь ли на экране YouTube?» — прямой вопрос к глазам: ищем текст в кадре,
    # а не в DOM и не в памяти. Срабатывает только когда назван знакомый объект:
    # иначе «посмотри, что тут» уходит старшему мозгу как сложная фраза.
    if re.search(VISION_VERBS, lowered):
        for word, needle in SCREEN_WORDS.items():
            if re.search(r"\b" + re.escape(word) + r"[а-яё]{0,2}\b", lowered):
                return {"action": "screen_look", "args": {"text": needle, "label": word},
                        "say": f"Смотрю на экран: видно ли {word}."}

    # «открой/запусти оперу» — живой браузер (сначала живой экземпляр владельца)
    if (re.search(r"\b(опер\w*|браузер\w*|browser)\b", lowered)
            and re.search(r"\b(открой|открыть|запусти|включи|подключи|open)\b", lowered)):
        return {"action": "browser_open", "args": {}, "say": "Открываю браузер."}

    # Сайт назван + команда открыть в ЛЮБОМ порядке слов: «найди на экране YouTube
    # и открой его» — живая речь не держит шаблона «открой X и найди Y».
    site = _site_in(lowered)
    if (site and not semantic_media
            and re.search(r"\b(открой|открыть|запусти|включи|включить|перейди|open)\b",
                          lowered)):
        return {"action": "navigate", "args": {"url": SITE_ALIASES[site]},
                "say": f"Открываю {site}."}

    # «найди …» на текущей странице. Но «найди в интернете … и ответь» — это не поиск по
    # странице, а задача со сбором и осмыслением: её отдаём старшему мозгу или цели
    # (живой прогон миссии 7: локальный поиск перехватывал фразу и отвечал «не нашлось поля»).
    web_hint = re.search(r"\b(в интернете|в сети|в гугле|погугли|из интернета)\b", lowered)
    match = re.search(r"\b(найди|поищи|search)\s+(.+)", lowered)
    if match and not web_hint and not semantic_media:
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
