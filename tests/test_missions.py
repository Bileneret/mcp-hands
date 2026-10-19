"""Тесты протокола живых миссий (Шаг 3): проверки факта, запись прогонов, сводка.

Протокол утверждён аудитом v26: «достигнуто» выставляется независимой проверкой
факта в мире, а не ответом ассистента. Здесь проверяется именно эта механика —
без живых окон и без сети: инструменты подменяются, проверки вызываются напрямую.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import missions  # noqa: E402


class FakeResult:
    def __init__(self, detail: dict, summary: str = "", ok: bool = True) -> None:
        self.detail, self.summary, self.ok = detail, summary, ok
        self.refused = False
        self.reason = ""


def test_mission_two_accepts_honest_negative(monkeypatch):
    """«Не вижу» при отсутствии текста — достигнуто: проверяется правдивость."""
    monkeypatch.setattr(missions.ms, "ocr_find",
                        lambda *a, **k: FakeResult({"found": False, "words_in_frame": 231}))
    ok, evidence = missions.check_youtube_visible({"say": "Нет, youtube на экране не вижу."})
    assert ok is True and "не вижу" in evidence


def test_mission_two_rejects_false_positive_and_false_negative(monkeypatch):
    """Ошибки в обе стороны ловятся: сказал «вижу», когда пусто, и наоборот."""
    monkeypatch.setattr(missions.ms, "ocr_find",
                        lambda *a, **k: FakeResult({"found": False, "words_in_frame": 10}))
    ok, evidence = missions.check_youtube_visible({"say": "Да, youtube видно на экране."})
    assert ok is False and "ответил" in evidence

    monkeypatch.setattr(missions.ms, "ocr_find",
                        lambda *a, **k: FakeResult({"found": True, "words_in_frame": 10,
                                                    "matches": [{"rect": [1, 2, 3, 4]}]}))
    monkeypatch.setattr(missions, "_window_at_point", lambda x, y: "'Opera'")
    ok, evidence = missions.check_youtube_visible({"say": "Нет, не вижу."})
    assert ok is False and "Opera" in evidence

    ok, evidence = missions.check_youtube_visible({"say": "Да, youtube видно."})
    assert ok is True and "Opera" in evidence


def test_mission_one_requires_window_named(monkeypatch):
    """Наблюдательная миссия: ответ обязан назвать активное окно (правдивость)."""
    monkeypatch.setattr(missions.ms, "observe",
                        lambda: FakeResult({"active": {"title": "Opera GX", "exe": "opera.exe"},
                                            "lines": [{"text": "строка"}]}))
    ok, _ = missions.check_screen_named({"say": "Активное окно: Opera GX. Строк 25."})
    assert ok is True
    ok, evidence = missions.check_screen_named({"say": "Готово: шагов 1 из 1."})
    assert ok is False and "Opera GX" in evidence


def test_mission_three_checks_real_page(monkeypatch):
    """Плитка: факт достижения — адрес настоящей страницы, а не слова ассистента."""
    monkeypatch.setattr(missions.ms, "live_page",
                        lambda: FakeResult({"url": "https://www.youtube.com/watch?v=1"}))
    assert missions.check_youtube_opened({})[0] is True
    monkeypatch.setattr(missions.ms, "live_page",
                        lambda: FakeResult({"url": "opera://startpage"}))
    ok, evidence = missions.check_youtube_opened({})
    assert ok is False and "startpage" in evidence


def test_run_mission_records_entry_and_report(tmp_path, monkeypatch):
    """Прогон пишет запись в JSONL, сводка строится по последней записи миссии."""
    log = tmp_path / "runs.jsonl"
    report = tmp_path / "MISSIONS_RUN.md"
    monkeypatch.setattr(missions, "RUN_LOG", log)
    monkeypatch.setattr(missions, "REPORT", report)

    class FakeAssistant:
        def handle(self, text: str) -> dict:
            return {"say": "Нет, youtube на экране не вижу.", "kind": "local", "detail": {}}

    monkeypatch.setattr(missions.ms, "ocr_find",
                        lambda *a, **k: FakeResult({"found": False, "words_in_frame": 5}))
    entry = missions.run_mission(2, FakeAssistant())
    assert entry["achieved"] is True and entry["route"] == "local"
    assert json.loads(log.read_text(encoding="utf-8").splitlines()[-1])["mission"] == 2

    target = missions.write_report()
    assert target == report
    text = report.read_text(encoding="utf-8")
    assert "Прогонов: 1 из 10; достигнуто: 1" in text, text
    assert "не запускалась" in text, "непройденные миссии остаются в таблице"


def test_mission_registry_matches_approved_list():
    """Реестр прогона обязан покрывать утверждённые владельцем миссии 1–10."""
    assert sorted(missions.MISSIONS) == list(range(1, 11))
    for number, mission in missions.MISSIONS.items():
        assert mission["phrase"] and callable(mission["verify"]), number
        assert mission["needs"], f"у миссии {number} не указано, что нужно для прогона"
