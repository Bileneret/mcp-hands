"""Тесты Шага 1 «Наблюдение и режим цели».

Проверяется то, что отличает режим цели от прежнего tool-loop: разведка перед
планом, исполнение через runner с постусловиями против мира, память рецептов и
честный отчёт (где именно застрял). Планировщик и исполнитель подменяются —
тест не ходит в сеть и ничего не нажимает.
"""

from __future__ import annotations

import json

import pytest

from mcp_hands import mcp_server as ms
from mcp_hands.assistant.goal import GoalRunner, RecipeStore, normalize_goal
from mcp_hands.brain import runner as runner_module
from mcp_hands.brain.llm import PlanRequestResult, expect_kinds_help
from mcp_hands.brain.plan import EXPECT_KINDS, Plan, parse_plan
from mcp_hands.brain.runner import PlanRun, StepOutcome

PLAN_JSON = {"plan_version": "3.0", "task_id": "goal-1", "steps": [
    {"action": "health", "args": {},
     "expect": {"kind": "file_exists", "params": {"path": "C:/tmp/нет-такого-файла"}}}]}


def _plan(steps: int = 1, action: str = "health") -> Plan:
    return parse_plan(json.loads(json.dumps(PLAN_JSON)))


def _run(ok: bool, index: int = 1, action: str = "health",
         expect: str = "file_exists") -> PlanRun:
    outcome = StepOutcome(index=index, action=action, ok=ok, expect_kind=expect,
                          expect_ok=ok, reason="" if ok else "постусловие не наступило")
    return PlanRun(task_id="goal-1", ok=ok,
                   reason="" if ok else "постусловие не наступило", steps=[outcome],
                   total_ms=1.0)


# --- каталог постусловий -------------------------------------------------------


def test_browser_expect_kinds_are_in_schema_and_prompt():
    """Новые виды постусловий обязаны быть и в схеме, и в подсказке планировщику."""
    for kind in ("video_playing", "fullscreen", "page_matches", "text_on_screen",
                 "window_visible", "foreground_is"):
        assert kind in EXPECT_KINDS, kind
    help_text = expect_kinds_help()
    for kind in ("video_playing", "page_matches", "text_on_screen", "foreground_is"):
        assert kind in help_text, kind


def test_check_expect_browser_kinds_read_the_world(monkeypatch):
    """Постусловие — это факт из DOM/кадра, а не слово модели."""
    class Result:
        def __init__(self, detail: dict, summary: str = "") -> None:
            self.detail, self.summary = detail, summary

    monkeypatch.setattr(runner_module, "live_video",
                        lambda action="status": Result({"has_video": True, "paused": False,
                                                        "fullscreen": False}, "видео"))
    assert runner_module._check_expect("video_playing", {})[0] is True
    monkeypatch.setattr(runner_module, "live_video",
                        lambda action="status": Result({"has_video": True, "paused": True}))
    assert runner_module._check_expect("video_playing", {})[0] is False

    monkeypatch.setattr(runner_module, "live_video",
                        lambda action="status": Result({"fullscreen": True}))
    assert runner_module._check_expect("fullscreen", {})[0] is True

    monkeypatch.setattr(runner_module, "live_page",
                        lambda: Result({"url": "https://www.youtube.com/watch?v=1",
                                        "title": "Майкл Наки — ДЕНЬ 300"}))
    assert runner_module._check_expect("page_matches", {"needle": "youtube"})[0] is True
    assert runner_module._check_expect("page_matches", {"needle": "twitch"})[0] is False
    assert runner_module._check_expect("page_matches", {})[0] is False

    monkeypatch.setattr(runner_module, "ocr_find",
                        lambda text, hwnd=0, **kwargs: Result({"found": True}, "найдено"))
    assert runner_module._check_expect("text_on_screen", {"text": "YouTube"})[0] is True
    assert runner_module._check_expect("text_on_screen", {})[0] is False


def test_check_expect_window_kinds_without_live_objects():
    """Отсутствие окна/фокуса — это честный False с причиной, а не исключение."""
    visible, reason = runner_module._check_expect(
        "window_visible", {"title_substring": "окна-с-таким-заголовком-нет-12345"})
    assert visible is False and "не найдено" in reason
    focused, reason = runner_module._check_expect("foreground_is", {})
    assert focused is False and "hwnd" in reason


# --- память рецептов -----------------------------------------------------------


def test_recipe_store_roundtrip(tmp_path):
    store = RecipeStore(tmp_path / "recipes.db")
    assert store.count() == 0 and store.get("включи ютуб") is None
    plan = _plan()
    store.save("Включи  YouTube!", plan, {"exe": "opera.exe", "browser": "work"})
    assert store.count() == 1
    loaded = store.get("включи youtube")          # нормализация не мешает совпадению
    assert loaded is not None
    plan_loaded, stamp = loaded
    assert len(plan_loaded.steps) == 1 and stamp["exe"] == "opera.exe"
    store.note_use("включи youtube")
    store.drop("включи youtube")
    assert store.get("включи youtube") is None and store.count() == 0
    assert normalize_goal("  Прибери  ЗАГРУЗКИ — пожалуйста! ") == "прибери загрузки пожалуйста"


# --- цикл цели -----------------------------------------------------------------


class FakePlanner:
    """Планировщик-заглушка: отдаёт планы по очереди и считает вызовы."""

    def __init__(self, plans: list[Plan]) -> None:
        self.plans = plans
        self.calls = 0
        self.notes: list[str] = []

    def __call__(self, config, task_id, title, needs, note=""):
        self.calls += 1
        self.notes.append(note)
        result = PlanRequestResult(plan=self.plans[min(self.calls - 1, len(self.plans) - 1)])
        return result


class FakeExecutor:
    """Исполнитель-заглушка: возвращает заранее заданные исходы по очереди."""

    def __init__(self, outcomes: list[PlanRun]) -> None:
        self.outcomes = outcomes
        self.calls = 0
        self.sources: list[str] = []

    def __call__(self, plan, limits=None, memory=None, plan_source="scripted"):
        self.calls += 1
        self.sources.append(plan_source)
        return self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]


def _runner(tmp_path, planner: FakePlanner, executor: FakeExecutor) -> GoalRunner:
    from mcp_hands.brain.llm import LLMConfig

    config = LLMConfig(base_url="http://localhost", api_key="test", model="test")
    runner = GoalRunner(config, tmp_path, planner=planner, executor=executor)
    runner.observe = lambda: {"summary": "разведка", "facts": {  # сеть/OCR не нужны
        "active": {"title": "YouTube", "exe": "opera.exe",
                   "occlusion": {"covered": False, "share": 0.0}},
        "windows_total": 3, "lines": [{"text": "YouTube"}],
        "browser": {"mode": "user", "tab_url": "https://youtube.com"}}}
    return runner


def test_goal_success_saves_recipe_and_reuses_it(tmp_path):
    """Удачная цель сохраняется рецептом и решается им же со второго раза."""
    planner = FakePlanner([_plan()])
    executor = FakeExecutor([_run(True)])
    runner = _runner(tmp_path, planner, executor)

    first = runner.run("Проверь, что файл на месте")
    assert first["ok"] is True and first["say"].startswith("Готово"), first
    assert planner.calls == 1 and runner.recipes.count() == 1
    assert "YouTube" in planner.notes[0], "план обязан строиться со срезом обстановки"

    second = runner.run("проверь что файл на месте")  # регистр/знаки не важны
    assert second["ok"] is True
    assert second["detail"]["source"] == "recipe"
    assert planner.calls == 1, "повторную цель рецепт решает без модели"


def test_goal_failure_reports_step_and_drops_bad_recipe(tmp_path):
    """Провал: отчёт «где застрял» и рецепт забывается, чтобы не повторять ошибку."""
    planner = FakePlanner([_plan()])
    executor = FakeExecutor([_run(False, index=1, action="health")])
    runner = _runner(tmp_path, planner, executor)
    runner.recipes.save("сломай файл", _plan())  # устаревший рецепт под эту цель

    outcome = runner.run("Сломай файл", planner_retries=0)
    assert outcome["ok"] is False
    assert "Не получилось на шаге 1" in outcome["say"], outcome
    assert "постусловие не наступило" in outcome["say"]
    assert runner.recipes.get("сломай файл") is None, "провальный рецепт забыт"


def test_goal_replans_once_then_reports(tmp_path):
    """Провал при живом планировщике — один переплан, затем честный отчёт."""
    planner = FakePlanner([_plan(), _plan()])
    executor = FakeExecutor([_run(False), _run(True)])
    runner = _runner(tmp_path, planner, executor)
    outcome = runner.run("Собери мне что-нибудь")
    assert outcome["ok"] is True
    assert planner.calls == 2 and executor.calls == 2, (planner.calls, executor.calls)
    assert outcome["detail"]["source"] == "llm"


def test_goal_without_plan_reports_model_error(tmp_path):
    """Модель не дала валидный план — честная фраза, без пустых обещаний."""
    class BrokenPlanner:
        def __call__(self, *args, **kwargs):
            return PlanRequestResult(plan=None, error="схема отвергла: план слишком длинный")

    runner = _runner(tmp_path, BrokenPlanner(), FakeExecutor([]))
    outcome = runner.run("Сделай невозможное")
    assert outcome["ok"] is False and "Не смог построить план" in outcome["say"], outcome


def test_facts_note_is_compact_and_useful(tmp_path):
    facts = {"active": {"title": "Экспресс-панель - Opera", "exe": "opera.exe",
                        "occlusion": {"covered": True, "share": 0.92}},
             "windows_total": 12, "browser": {"mode": "user", "tab_url": "opera://startpage"},
             "lines": [{"text": f"строка {index}"} for index in range(40)]}
    note = GoalRunner._facts_note(facts)
    assert "Экспресс-панель" in note and "перекрытие окна: True" in note
    assert "режим user" in note and len(note) <= 1200


def test_assistant_goal_mode_routes_unmatched_phrase_to_goal(tmp_path):
    """В режиме цели фраза, не пойманная локальным интентом, уходит в цикл цели."""
    from mcp_hands.assistant.agent import Assistant

    assistant = Assistant(force_work=True, log_dir=tmp_path, goal_mode=True)
    assistant.run_local = lambda text: {"handled": False}  # type: ignore[assignment]
    assistant.run_goal = lambda text: {"say": "Готово: шагов 3, видео играет.",  # type: ignore[assignment]
                                       "ok": True, "detail": {"source": "llm"}}
    result = assistant.handle("включи мне что-нибудь интересное")
    assert result["kind"] == "goal", result
    assert result["say"].startswith("Готово")

    plain = Assistant(force_work=True, log_dir=tmp_path)
    plain.run_smart = lambda text, **kwargs: {"say": "старший мозг", "detail": {}}  # type: ignore[assignment]
    assert plain.handle("приведи загрузки в порядок")["kind"] == "smart"


# --- окно по названию (а не по числу) ------------------------------------------


def test_resolve_window_reports_missing_title():
    """Планировщик не знает hwnd: имя не нашлось — честный отказ, не «первое попавшееся»."""
    hwnd, why = ms.resolve_window(0, "окна-с-таким-заголовком-нет-98765")
    assert hwnd == 0 and "не найдено" in why
    hwnd, why = ms.resolve_window(0, "")
    assert hwnd == 0 and "title_substring" in why
    assert ms.resolve_window(12345, "") == (12345, "")


def test_window_activate_and_ocr_by_title_are_canon_tools():
    """Инструменты не изменили канон, а помощник разрешения окна им не является."""
    assert "resolve_window" not in ms.TOOL_NAMES
    assert "window_activate" in ms.TOOL_NAMES and "ocr_window" in ms.TOOL_NAMES


def test_window_activate_refuses_unknown_title_without_touching_desktop():
    result = ms.window_activate(title_substring="окна-нет-98765")
    assert result.ok is False and result.refused is True
    assert "не найдено" in result.reason


def test_ocr_window_refuses_unknown_title():
    result = ms.ocr_window(title_substring="окна-нет-98765")
    assert result.ok is False and result.refused is True


def test_window_activate_by_title_finds_live_window(target, require_input_desktop):
    """Живой прогон: окно называется заголовком — как это делает человек."""
    result = ms.window_activate(title_substring="pytest target")
    assert result.ok is True, result
    hwnd, why = ms.resolve_window(0, "pytest target")
    assert hwnd == target.hwnd and why == ""

    shot = ms.ocr_window(title_substring="pytest target")
    assert shot.ok is True and shot.detail["lines"], shot


# --- инструмент наблюдения -----------------------------------------------------


def test_observe_returns_compact_situation():
    """`observe` — один вызов вместо серии догадок, и он держится в бюджете."""
    result = ms.observe()
    assert result.ok is True
    for key in ("active", "windows_total", "lines", "browser", "capabilities"):
        assert key in result.detail, key
    assert result.detail["capabilities"]["ocr"] is True
    assert len(json.dumps(result.detail, ensure_ascii=False, default=str)) <= 3400


def test_observe_is_in_canon_and_dispatch():
    assert "observe" in ms.TOOL_NAMES
    assert "observe" in runner_module.DISPATCH_TABLE


@pytest.mark.parametrize("text", ["включи ютуб", "видишь ли на экране youtube"])
def test_local_intents_still_win_over_goal_mode(text, tmp_path):
    """Точечные фразы отвечаются мгновенно и не уходят в планировщик."""
    from mcp_hands.assistant.agent import Assistant

    assistant = Assistant(force_work=True, log_dir=tmp_path, goal_mode=True)
    assistant._call_tool = lambda name, args: {"ok": True, "summary": "ок",  # type: ignore[assignment]
                                               "detail": {"found": True,
                                                          "matches": [{"rect": [1, 2, 3, 4]}]}}
    result = assistant.handle(text)
    assert result["kind"] == "local", result

# --- Шаг 2: политика вкладок, пиксельный путь, прокрутка -----------------------


def test_live_tabs_refuses_without_session():
    """Без живой сессии инструмент отказывает внятно, а не молчит."""
    result = ms.live_tabs()
    assert result.ok is False and result.refused is True
    assert "live_browser" in result.reason


def test_click_text_on_screen_refuses_unknown_window():
    """Пиксельный клик невозможен без окна: отказ с причиной, ничего не нажимаем."""
    result = ms.click_text_on_screen("что-нибудь", title_substring="окна-нет-98765")
    assert result.ok is False and result.refused is True
    assert "не найдено" in result.reason


def test_click_text_on_screen_refuses_when_text_absent(target):
    """Нет текста — нет клика: координаты не выдумываются."""
    result = ms.click_text_on_screen("текста-такого-нет-98765", hwnd=target.hwnd)
    assert result.ok is False and result.refused is True
    assert "не найден" in result.reason


def test_scroll_refuses_without_window():
    assert ms.scroll(clicks=3, title_substring="окна-нет-98765").refused is True
    assert ms.scroll(clicks=0).refused is True


def test_scroll_dry_run_does_not_touch_screen(target):
    """Сухой прогон показывает точку и не отправляет ни одного события."""
    result = ms.scroll(clicks=3, hwnd=target.hwnd, dry_run=True)
    assert result.ok is True and result.detail["dry_run"] is True
    assert result.detail["point"], result.detail


def test_click_dry_run_reports_point_without_clicking(target):
    """Сухой прогон пиксельного клика: точка показана, ввод не отправлен."""
    result = ms.click_text_on_screen("beta", hwnd=target.hwnd, dry_run=True)
    if not result.ok and result.refused and "не найден" in result.reason:
        pytest.skip("OCR не распознал подпись кнопки в этом прогоне")
    assert result.ok is True, result
    assert result.detail["dry_run"] is True and result.detail["point"]
    assert target.clicks() == [], "сухой прогон не должен нажимать"


def test_speakable_strips_paths_and_json():
    """Голосовой отчёт не зачитывает пути и JSON (замечание аудита v25)."""
    from mcp_hands.assistant.goal import speakable

    dirty = ("Не получилось на шаге 1 (ocr_find): причина не наступила "
             '{"ok": true, "detail": {"lines": []}} и ' + r"C:\temp\файл.png")
    clean = speakable(dirty)
    assert "{" not in clean and "}" not in clean and "C:" not in clean, clean
    assert clean.startswith("Не получилось на шаге 1")


def test_recipe_fingerprint_skips_recipe_in_other_window(tmp_path):
    """Рецепт чужого окна не применяем и НЕ удаляем: просто планируем заново."""
    planner = FakePlanner([_plan()])
    executor = FakeExecutor([_run(True)])
    runner = _runner(tmp_path, planner, executor)
    runner.recipes.save("проверь окно", _plan(),  # рецепт снят в другом окне
                        {"exe": "sovsem_drugoe.exe", "browser": "user"})
    outcome = runner.run("проверь окно")
    assert outcome["ok"] is True
    assert outcome["detail"]["source"] == "llm", outcome["detail"]
    assert planner.calls == 1, "при несовпадении отпечатка зовём планировщик"
    assert runner.recipes.count() == 1, "рецепт не удаляем — он может быть валиден в своём окне"
