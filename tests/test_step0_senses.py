"""Тесты Шага 0 «глаза по запросу»: окклюзия окон, поиск текста, бюджеты ответов.

Разбор случая владельца 19.10 («YouTube есть на экране, а видимости нет») дал три
дефекта, и каждый здесь проверяется отдельно: усечение больших списков съедало
факты, не было инструмента «найти текст на экране», а захват окна читает
прямоугольник ЭКРАНА — при перекрытии это чужое содержимое, и об этом надо
сообщать, а не выдумывать.
"""

from __future__ import annotations

import pytest

from mcp_hands import mcp_server as ms
from mcp_hands.assistant.agent import TOOL_PAYLOAD_LIMIT, tool_payload
from mcp_hands.assistant.router import route
from mcp_hands.assistant.session import is_stop_phrase
from mcp_hands.geometry import intersection_area, occlusion_report, rect_area
from mcp_hands.ocr import available

RECT = {"left": 100, "top": 100, "right": 500, "bottom": 400}  # 400×300 = 120000


def test_rect_helpers_count_intersection():
    assert rect_area(RECT) == 120_000
    assert intersection_area(RECT, RECT) == 120_000
    assert intersection_area(RECT, {"left": 0, "top": 0, "right": 50, "bottom": 50}) == 0
    half = {"left": 100, "top": 100, "right": 500, "bottom": 250}
    assert intersection_area(RECT, half) == 60_000  # ровно половина площади


def test_occlusion_report_says_who_covers_and_how_much():
    """Перекрытие — это факт с именем виновника, а не догадка «вроде видно»."""
    quiet = occlusion_report(RECT, [])
    assert quiet == {"covered": False, "share": 0.0, "by": []} or quiet["covered"] is False

    on_top = occlusion_report(RECT, [(777, "Совсем другое окно", RECT)])
    assert on_top["covered"] is True and on_top["share"] == 1.0
    assert on_top["by"][0]["hwnd"] == 777 and on_top["by"][0]["title"].startswith("Совсем")

    corner = occlusion_report(RECT, [(1, "уголок", {"left": 100, "top": 100,
                                                    "right": 150, "bottom": 150})])
    assert corner["covered"] is False  # 1/48 площади — не повод говорить «перекрыто»


def test_occlusion_of_real_window_is_reported():
    """На живой системе функция обязана вернуть вердикт, а не упасть.

    В безголовом окружении (фоновый раннер, CI) видимых окон нет — это отказ среды,
    а не поломка кода, поэтому тест пропускается с причиной (замечание аудита v24).
    """
    from mcp_hands.geometry import window_occlusion

    windows = (ms.windows_list().detail or {}).get("windows", [])
    if not windows:
        pytest.skip("в этой сессии нет видимых окон (безголовое окружение)")
    facts = window_occlusion(int(windows[0]["hwnd"]))
    for key in ("covered", "share", "by", "minimized"):
        assert key in facts, facts


def test_windows_list_query_is_point_filter():
    """Точечный вопрос «открыт ли Блокнот» не зависит от числа остальных окон."""
    rows = [{"hwnd": 1, "title": "Блокнот — заметки", "exe": "notepad.exe"},
            {"hwnd": 2, "title": "Opera GX", "exe": "opera.exe"},
            {"hwnd": 3, "title": "Документ1 — Word", "exe": "WINWORD.EXE"}]
    assert ms.filter_windows(rows, "") == rows
    assert [row["hwnd"] for row in ms.filter_windows(rows, "блокнот")] == [1]
    assert [row["hwnd"] for row in ms.filter_windows(rows, "OPERA")] == [2]
    assert [row["hwnd"] for row in ms.filter_windows(rows, "winword")] == [3]
    assert ms.filter_windows(rows, "нет-такого-окна") == []


def test_windows_list_summary_mentions_filter():
    result = ms.windows_list(query="заведомонесуществующееокно12345")
    assert result.ok is True
    assert "0 из" in result.summary, result.summary
    assert result.detail["found"] == 0


def test_tool_payload_keeps_facts_beyond_old_twelve_items():
    """Раньше список ужимался до 12 записей — теперь отдаётся всё, что влезает."""
    small = [{"text": f"слово{index}", "rect": [index, index, index + 10, index + 10]}
             for index in range(30)]
    payload = tool_payload({"ok": True, "summary": "слов: 30", "reason": "",
                            "detail": {"words": small}})
    assert "слово29" in payload, "умеренный список обязан доезжать целиком"
    assert len(payload) <= TOOL_PAYLOAD_LIMIT + len(" (обрезано)")

    huge = [{"text": f"запись{index}", "rect": [index] * 4} for index in range(400)]
    capped = tool_payload({"ok": True, "summary": "записей: 400", "reason": "",
                           "detail": {"lines": huge}})
    assert "запись40" in capped, "усечение идёт по знакам, а не по 12 записям"


def test_tool_payload_still_caps_absurd_answers():
    words = [{"text": "x" * 200} for _ in range(500)]
    payload = tool_payload({"ok": True, "summary": "много", "detail": {"words": words}})
    assert len(payload) <= TOOL_PAYLOAD_LIMIT + len(" (обрезано)")


def test_stop_phrases_are_tokenized():
    """«Джарвис, стоп!» и «хватит, пожалуйста» — команды; «стоп видео» — нет."""
    assert is_stop_phrase("Стоп")
    assert is_stop_phrase("Джарвис, стоп!")
    assert is_stop_phrase("хватит, пожалуйста")
    assert is_stop_phrase("Джарвис, спать, пожалуйста")
    assert not is_stop_phrase("стоп видео")
    assert not is_stop_phrase("останови видео")
    assert not is_stop_phrase("хватит болтать")
    assert not is_stop_phrase("")


def test_router_answers_vision_questions_locally():
    """«Посмотри, увидишь на экране YouTube» — вопрос к глазам, а не к странице."""
    intent = route("Посмотри, увидишь на экране YouTube.")
    assert intent and intent["action"] == "screen_look", intent
    assert intent["args"]["text"] == "youtube"
    opera = route("видишь ли на экране оперу")
    assert opera["action"] == "screen_look" and opera["args"]["text"] == "opera"
    # сложные «посмотри, что тут» уходят старшему мозгу, а не в OCR-поиск
    assert route("посмотри, что тут происходит") is None
    # и старые интенты не сломаны
    assert route("открой ютуб")["action"] == "navigate"


def test_screen_look_speaks_the_actual_fact(tmp_path):
    """Ответ строится по факту кадра: «да, видно» или «нет, не вижу» + перекрытие."""
    from mcp_hands.assistant.agent import Assistant

    assistant = Assistant(force_work=True, log_dir=tmp_path)
    answers = [
        {"ok": True, "summary": "найдено", "detail": {"found": True,
                                                      "matches": [{"rect": [10, 20, 30, 40]}]}},
        {"ok": True, "summary": "НЕ НАЙДЕНО", "detail": {
            "found": False,
            "occlusion": {"covered": True, "share": 0.9,
                          "by": [{"title": "Совсем другое окно"}]}}},
    ]
    assistant._call_tool = lambda name, args: answers.pop(0)  # type: ignore[assignment]
    first = assistant.run_local("Посмотри, увидишь на экране YouTube.")
    assert first["say"].startswith("Да, youtube видно"), first
    second = assistant.run_local("Посмотри, увидишь на экране YouTube.")
    assert second["say"].startswith("Нет, youtube на экране не вижу"), second
    assert "закрыта другим окном" in second["say"], second


# --- живые проверки: окно-мишень, OCR и UIA -----------------------------------


def test_element_find_reports_not_found_instead_of_silent_ok(target):
    """«Не найдено» — отдельный вердикт, а не «успех без подробностей»."""
    missing = ms.element_find(hwnd=target.hwnd, name="такого-элемента-нет-12345")
    assert missing.ok is True
    assert missing.summary.startswith("НЕ НАЙДЕНО"), missing.summary
    assert missing.detail["status"] == "not_found" and missing.detail["found"] is False

    known = ms.element_find(hwnd=target.hwnd, name="beta", scope="descendants")
    assert known.detail.get("status") in ("found", "not_found", None), known.detail
    if known.detail.get("status") == "found":
        assert known.summary.startswith("найдено")


def test_content_map_failure_gives_advice_instead_of_raw_error(monkeypatch):
    """Обход дерева Chromium может отваливаться — это вердикт с советом, не сбой."""
    class Broken:
        def walk(self, *_args, **_kwargs):
            raise RuntimeError("Событие не смогло вызвать ни одного из абонентов")

    monkeypatch.setattr(ms, "_automation", lambda: Broken())
    result = ms.content_map(hwnd=123456)
    assert result.ok is False
    assert "element_find" in result.reason and "ocr_find" in result.reason, result.reason


pytestmark_ocr = pytest.mark.skipif(not available(), reason="в системе нет движка Windows OCR")


@pytestmark_ocr
def test_ocr_find_answers_yes_and_no_on_the_target_window(target, require_capture):
    """Прямой ответ на «видишь X?»: нашлось — координаты, нет — честное «НЕ НАЙДЕНО»."""
    found_any = []
    for label in ("alpha", "beta", "gamma"):
        result = ms.ocr_find(label, hwnd=target.hwnd)
        assert result.detail["scope"].startswith("окно")
        if result.detail["found"]:
            found_any.append(label)
            assert "найдено" in result.summary
            rect = result.detail["matches"][0]["rect"]
            assert rect[2] > rect[0] and rect[3] > rect[1], rect
    assert found_any, "ни одна подпись кнопок не распозналась — OCR сломан?"

    absent = ms.ocr_find("нет-такого-текста-999", hwnd=target.hwnd)
    assert absent.detail["found"] is False
    assert absent.summary.startswith("НЕ НАЙДЕНО"), absent.summary
    assert "occlusion" in absent.detail


@pytestmark_ocr
def test_ocr_window_reports_lines_and_occlusion(target, require_capture):
    """Строки вместо простыни слов + признак перекрытия окна в том же ответе."""
    result = ms.ocr_window(hwnd=target.hwnd)
    assert result.ok is True
    assert result.detail["lines"], result.detail
    assert result.detail["occlusion"]["covered"] in (True, False)
    filtered = ms.ocr_window(hwnd=target.hwnd, query="альфа")
    assert filtered.detail["total_words"] >= filtered.detail["word_count"]
