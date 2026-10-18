"""Тесты живого слоя (CDP к Opera) — без внешнего веба.

Живой YouTube-сценарий проверяется приёмкой вручную (spike/live_youtube_probe.py)
и прогоном через MCP: в pytest идёт только локальная страница `data:` — запуск
браузера, вкладка, навигация, чтение страницы, скриншот, остановка дерева.
При отсутствии Opera тест пропускается с причиной.

Отдельная группа — самолечение соединения: обрыв связи (в логе владельца
WinError 10053 при попытке открыть YouTube) проверяется на фейковых сокетах,
без браузера.
"""

from __future__ import annotations

import json

import pytest
import websocket

from mcp_hands.live import LiveBrowser, opera_path
from mcp_hands.scenarios.browser_links import Cdp, CdpTransportError

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


# --- самолечение соединения (без браузера: фейковые сокеты) ---------------------


class FakeSocket:
    """Сокет отладочной точки: либо мёртвый на отправке, либо отвечающий."""

    def __init__(self, broken: bool) -> None:
        self.broken = broken
        self.sent: list[dict] = []
        self.closed = False

    def send(self, payload: str) -> None:
        if self.broken:
            raise ConnectionAbortedError(10053, "Программа на хост-компьютере разорвала "
                                                "установленное подключение")
        self.sent.append(json.loads(payload))

    def recv(self) -> str:
        return json.dumps({"id": self.sent[-1]["id"], "result": {"ok": True}})

    def close(self) -> None:
        self.closed = True


class FakeSilentSocket(FakeSocket):
    """Сокет, который принимает команду, но не отвечает (таймаут отклика)."""

    def recv(self) -> str:
        raise websocket.WebSocketTimeoutException("timed out")


def _install_sockets(monkeypatch, factory) -> list:
    created: list[FakeSocket] = []

    def create(url, timeout=None, suppress_origin=False):
        socket = factory(len(created))
        created.append(socket)
        return socket

    monkeypatch.setattr("websocket.create_connection", create)
    return created


def test_cdp_reconnects_once_and_repeats_command(monkeypatch):
    """Обрыв соединения: переподключение по тому же адресу и ОДИН повтор команды."""
    created = _install_sockets(monkeypatch, lambda index: FakeSocket(broken=index == 0))
    cdp = Cdp(9, timeout_s=1.0)
    cdp.connect("ws://127.0.0.1:9/tab")
    assert cdp.call("Runtime.evaluate", expression="1") == {"ok": True}
    assert len(created) == 2, "ожидалось одно переподключение"
    assert created[1].sent[0]["method"] == "Runtime.evaluate"


def test_cdp_dead_address_reports_broken_link(monkeypatch):
    """Адрес сессии мёртв (вкладку закрыли): понятная ошибка связи, не «сырой» сокет."""
    _install_sockets(monkeypatch, lambda index: FakeSocket(broken=True))
    cdp = Cdp(9, timeout_s=1.0)
    cdp.connect("ws://127.0.0.1:9/tab")
    with pytest.raises(CdpTransportError) as error:
        cdp.call("Runtime.evaluate", expression="1")
    assert "обрыв связи" in str(error.value), error.value


def test_cdp_timeout_does_not_repeat_command(monkeypatch):
    """Таймаут отклика: повтора НЕТ — команда могла уже исполниться в браузере."""
    created = _install_sockets(monkeypatch, lambda index: FakeSilentSocket(broken=False))
    cdp = Cdp(9, timeout_s=1.0)
    cdp.connect("ws://127.0.0.1:9/tab")
    with pytest.raises(CdpTransportError) as error:
        cdp.call("Page.navigate", url="https://example.org")
    assert "обрыв связи" in str(error.value)
    assert len(created) == 1, "повтор на таймауте недопустим"


class FakeCdp:
    """Клиент, который сначала рвёт связь, а после перепривязки отвечает."""

    def __init__(self, failures: int = 1) -> None:
        self.failures = failures
        self.methods: list[str] = []
        self.connected_to = ""
        self.tabs = [{"id": "tab-1", "type": "page",
                      "webSocketDebuggerUrl": "ws://127.0.0.1:9/tab-1"}]

    def call(self, method: str, **params) -> dict:
        if self.failures:
            self.failures -= 1
            raise CdpTransportError("обрыв связи с вкладкой (ConnectionAbortedError)")
        self.methods.append(method)
        return {"result": {"value": "после переподключения"}}

    def endpoint(self, path: str = "/json/version") -> dict:
        return self.tabs

    def connect(self, ws_url: str) -> None:
        self.connected_to = ws_url

    def close(self, keep_url: bool = False) -> None:
        return None


def test_live_call_reattaches_tab_after_break():
    """Живой слой сам перепривязывается к ТОЙ ЖЕ вкладке и повторяет операцию."""
    browser = LiveBrowser()
    browser.cdp = FakeCdp()
    browser.mode, browser.port = "work", 9
    browser.tab = {"id": "tab-1", "webSocketDebuggerUrl": "ws://127.0.0.1:9/tab-1"}
    assert browser.evaluate("1 + 1") == "после переподключения"
    assert browser.cdp.connected_to == "ws://127.0.0.1:9/tab-1", browser.cdp.connected_to
    assert browser.cdp.methods == ["Page.enable", "Runtime.evaluate"], browser.cdp.methods


def test_live_call_reports_when_reattach_impossible():
    """Вкладки больше нет и открыть нечего: ошибка связи с внятным текстом."""
    browser = LiveBrowser()
    browser.cdp = FakeCdp(failures=99)
    browser.cdp.tabs = []
    browser.tab = {"id": "tab-1", "webSocketDebuggerUrl": "ws://127.0.0.1:9/tab-1"}
    with pytest.raises(CdpTransportError) as error:
        browser.evaluate("1 + 1")
    assert "повтор не удался" in str(error.value), error.value


def test_live_call_converts_dead_port_to_broken_link():
    """Порт мёртв (браузер сняли извне): наружу идёт ошибка связи, а не URLError.

    Именно этот путь давал в живой проверке «Не вышло: URLError: [WinError 10061]»:
    перепривязка к вкладке обращается к отладочному порту, а его уже нет.
    """
    from urllib.error import URLError

    browser = LiveBrowser()
    browser.cdp = FakeCdp(failures=99)
    browser.mode, browser.port = "work", 9
    browser.tab = {"id": "tab-1", "webSocketDebuggerUrl": "ws://127.0.0.1:9/tab-1"}

    def dead_endpoint(path: str = "/json/list"):
        raise URLError(ConnectionRefusedError(10061, "Подключение не установлено"))

    browser.cdp.endpoint = dead_endpoint  # type: ignore[method-assign]
    with pytest.raises(CdpTransportError) as error:
        browser.evaluate("1 + 1")
    assert "обрыв связи" in str(error.value), error.value


def test_live_status_without_own_tab_opens_nothing():
    """«Видишь Оперу?» в user-режиме не открывает лишних вкладок владельцу."""
    browser = LiveBrowser()
    browser.mode, browser.port = "user", 9222
    browser.tab = {}
    facts = browser.status()
    assert facts["note"] == "рабочая вкладка ещё не открыта"
    assert browser.cdp is None

def test_work_profile_tab_policy(tmp_path):
    """Политика вкладок на своём профиле: список, привязка к указанной, честный отказ.

    Живой тест включается только там, где есть Opera: в остальных средах он
    пропускается по той же причине, что и остальные живые проверки слоя.
    """
    if not opera_path():
        pytest.skip("opera.exe не найден — живой слой недоступен в этой среде")
    browser = LiveBrowser()
    try:
        browser.start_work(headless=True)
        browser.ensure_tab()
        rows = browser.tabs()
        assert rows and all("id" in row for row in rows), rows
        picked = browser.attach_tab(tab_id=str(rows[0]["id"]))
        assert picked.get("ok") is True, picked
        assert "focused" in picked
        missing = browser.attach_tab(title_substring="нет-такой-вкладки-98765")
        assert missing.get("ok") is False and "нет" in missing["reason"], missing
    finally:
        stopped = browser.stop()
        assert stopped.get("stopped") is True, stopped
