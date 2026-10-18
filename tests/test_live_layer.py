"""Тесты живого слоя (CDP к Opera) — без внешнего веба.

Живой YouTube-сценарий проверяется приёмкой вручную (spike/live_youtube_probe.py)
и прогоном через MCP: в pytest идёт только локальная страница `data:` — запуск
браузера, вкладка, навигация, чтение страницы, скриншот, остановка дерева.
При отсутствии Opera тест пропускается с причиной.
"""

from __future__ import annotations

import pytest

from mcp_hands.live import LiveBrowser, opera_path

DATA_PAGE = ("data:text/html,<title>Probe</title><h1>hello</h1>"
             "<video id='player'></video><input type='search' name='q'>")


def test_live_work_profile_local_page(tmp_path):
    if not opera_path():
        pytest.skip("opera.exe не найден — живой слой недоступен в этой среде")
    browser = LiveBrowser()
    try:
        started = browser.start_work(headless=True)
        assert started.get("port"), started
        tab = browser.ensure_tab()
        assert tab.get("tab"), tab
        navigation = browser.navigate(DATA_PAGE)
        assert navigation.get("loaded"), navigation
        info = browser.page_info()
        assert info and "Probe" in (info.get("title") or ""), info
        assert info.get("videos") == 1 and info.get("search_inputs"), info
        shot = browser.screenshot(str(tmp_path / "проба.png"))
        assert shot.get("ok") and shot.get("bytes", 0) > 500, shot
    finally:
        stopped = browser.stop()
        assert stopped.get("stopped") is True, stopped


def test_user_mode_reports_missing_port_clearly(tmp_path, monkeypatch):
    """User-режим без отладочного порта: внятный отказ с инструкцией, без действий.

    Порт подменяется заведомо закрытым (9/discard): тест не зависит от того,
    поднят ли у владельца отладочный порт на самом деле.
    """
    import mcp_hands.live as live_mod

    monkeypatch.setattr(live_mod, "USER_PORT_FILE", tmp_path / "нет_порта.txt")
    monkeypatch.setattr(live_mod, "DEFAULT_USER_PORT", 9)
    browser = LiveBrowser()
    outcome = browser.connect_user()
    assert outcome["ok"] is False
    assert "opera_debug_launcher" in outcome["reason"]
    assert browser.mode == ""  # ничего не подключено и не тронуто
