"""Сценарий «ссылки из браузера»: браузерный путь через CDP.

Инжекции ввода здесь нет: браузер запускается с отладочным портом, страница читается
через протокол, ссылки извлекаются запросом к DOM. Окно не показывается вовсе
(новый headless-режим), чтобы сценарий не мешал работе на машине.

Профиль — отдельный каталог в рабочей папке прогона: пользовательский профиль
не используется, чужие сессии и закладки не читаются.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

import websocket  # websocket-client

from .runner import Run

EDGE_PATHS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
PAGE_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>Стенд ссылок</title></head>
<body>
  <h1>Источники</h1>
  <ul>
    <li><a class="resource" href="https://example.org/alpha">Alpha: первый источник</a></li>
    <li><a class="resource" href="https://example.org/beta">Beta: второй источник</a></li>
    <li><a class="resource" href="https://example.org/gamma">Gamma: третий источник</a></li>
  </ul>
</body></html>
"""
EXTRACT_JS = ("JSON.stringify(Array.from(document.querySelectorAll('a.resource'))"
              ".map(function(a){return {title: a.textContent.trim(), url: a.href};}))")


def free_port() -> int:
    """Свободный локальный порт: фиксированный номер занимает не успевший закрыться браузер."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def edge_path() -> str | None:
    for candidate in EDGE_PATHS:
        if Path(candidate).exists():
            return candidate
    return None


class Cdp:
    """Минимальный клиент протокола: одна сессия страницы, вызовы команд."""

    def __init__(self, port: int, timeout_s: float = 20.0) -> None:
        self.port = port
        self.timeout_s = timeout_s
        self.ws = None
        self.counter = 0

    def endpoint(self, path: str = "/json/version") -> dict:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def wait_ready(self) -> dict:
        deadline = time.time() + self.timeout_s
        last = ""
        while time.time() < deadline:
            try:
                return self.endpoint("/json/version")
            except Exception as exc:  # noqa: BLE001 — порт поднимается не мгновенно
                last = str(exc)
                time.sleep(0.3)
        raise TimeoutError(f"отладочный порт Edge не поднялся: {last}")

    def open_page(self, url: str, attempts: int = 8) -> dict:
        """Открывает вкладку и возвращает её отладочную точку.

        Сразу после старта браузер отвечает на /json/version, но список вкладок ещё
        не готов: /json/new может вернуть ошибку. Поэтому повторы.
        """
        query = urllib.parse.quote(url, safe="")
        last = ""
        for attempt in range(1, attempts + 1):
            for method in ("PUT", "GET"):
                try:
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{self.port}/json/new?{query}", method=method)
                    with urllib.request.urlopen(request, timeout=10) as response:
                        return json.loads(response.read().decode("utf-8"))
                except Exception as exc:  # noqa: BLE001 — пробуем следующий способ и повтор
                    last = f"{method}: {exc}"
            time.sleep(0.4)
        raise TimeoutError(f"вкладка не открылась за {attempts} попыток: {last}")

    def connect(self, ws_url: str) -> None:
        # suppress_origin: заголовок Origin не отправляем вовсе — так соединение
        # принимается независимо от настроек --remote-allow-origins.
        self.ws = websocket.create_connection(ws_url, timeout=self.timeout_s,
                                              suppress_origin=True)

    def call(self, method: str, **params) -> dict:
        self.counter += 1
        self.ws.send(json.dumps({"id": self.counter, "method": method, "params": params}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == self.counter:
                if "error" in message:
                    raise RuntimeError(f"CDP {method}: {message['error']}")
                return message.get("result", {})

    def close(self) -> None:
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:  # noqa: BLE001
                pass
            self.ws = None


def kill_our_browsers(profile: Path) -> int:
    """Снимает процессы браузера, запущенные с НАШИМ профилем.

    Headless-браузер оставляет после себя дочерние процессы (рендерер, GPU, crashpad):
    они держат файлы профиля, и следующий запуск не поднимается. Отбор строго по пути
    профиля, поэтому браузер пользователя не затрагивается.
    """
    command = (
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{profile}*' }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }"
    )
    try:
        completed = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                                   capture_output=True, timeout=30)
    except Exception:  # noqa: BLE001 — уборка не должна ломать сценарий
        return 0
    return len([line for line in completed.stdout.decode("utf-8", "replace").split() if line.isdigit()])


def launch_browser(browser: str, profile: Path, attempts: int = 3) -> tuple[subprocess.Popen, "Cdp"]:
    """Запускает браузер с повторами: холодный старт иногда не отдаёт порт вовремя.

    Каждая попытка — свой свободный порт: номер, оставшийся от предыдущего экземпляра,
    отвечает, но уже мёртв, и открыть вкладку не даёт.
    """
    last = ""
    for attempt in range(1, attempts + 1):
        if attempt > 1 or kill_our_browsers(profile):
            time.sleep(1.5)
        port = free_port()
        cdp = Cdp(port, timeout_s=30.0)
        process = subprocess.Popen([
            browser, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
            # Chromium с версии 111 отклоняет отладочный WebSocket с «чужим» Origin
            # (403 Forbidden), поэтому источник разрешается явно.
            "--remote-allow-origins=*",
            "--headless=new", "--no-first-run", "--no-default-browser-check",
            "--disable-extensions", "--disable-popup-blocking", "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            cdp.wait_ready()
            return process, cdp
        except Exception as exc:  # noqa: BLE001 — пробуем ещё раз
            last = f"попытка {attempt}: {exc}"
            process.kill()
            process.wait(timeout=10)
            time.sleep(1.0)
    raise TimeoutError(f"браузер не поднялся: {last}")


def scenario(run: Run, workdir: Path) -> None:
    browser = edge_path()
    if browser is None:
        with run.step("найти браузер", expect="Edge установлен") as step:
            step.error = "Edge не найден ни по одному известному пути"
        return

    page = workdir / "стенд_ссылок.html"
    page.write_text(PAGE_HTML, encoding="utf-8")
    result_file = workdir / "ссылки.json"
    profile = workdir / "profile"

    process = None
    cdp = None
    try:
        with run.step("запустить Edge с отладочным портом", expect="порт отвечает",
                      method="процесс + отдельный профиль + headless, с повторами") as step:
            try:
                process, cdp = launch_browser(browser, profile)
            except TimeoutError as exc:
                step.error = str(exc)
                return
            version = cdp.wait_ready()
            step.details.update({"browser": version.get("Browser"), "pid": process.pid,
                                 "port": cdp.port})
            run.note(debug_port=cdp.port)
            run.confirm(f"{version.get('Browser')} на порту {cdp.port}", ok=bool(version))

        with run.step("открыть страницу стенда", expect="вкладка создана",
                      method="CDP: /json/new") as step:
            tab = cdp.open_page(page.as_uri())
            step.details["tab"] = {"id": tab.get("id"), "url": tab.get("url")}
            run.confirm(f"вкладка {tab.get('id')}", ok=bool(tab.get("webSocketDebuggerUrl")))
            if not tab.get("webSocketDebuggerUrl"):
                step.error = "не получена отладочная точка вкладки"
                return

        with run.step("дождаться загрузки страницы", expect="document.readyState = complete",
                      method="CDP: Page.enable + опрос readyState") as step:
            cdp.connect(tab["webSocketDebuggerUrl"])
            cdp.call("Page.enable")
            deadline = time.time() + 15
            state = ""
            while time.time() < deadline:
                state = cdp.call("Runtime.evaluate",
                                 expression="document.readyState",
                                 returnByValue=True)["result"].get("value", "")
                if state == "complete":
                    break
                time.sleep(0.2)
            step.details["ready_state"] = state
            run.confirm(f"состояние {state}", ok=state == "complete")
            if state != "complete":
                step.error = f"страница не загрузилась: {state}"

        with run.step("собрать ссылки по селектору", expect="три ссылки с адресами и текстом",
                      method="CDP: Runtime.evaluate над DOM") as step:
            raw = cdp.call("Runtime.evaluate", expression=EXTRACT_JS,
                           returnByValue=True)["result"].get("value", "[]")
            links = json.loads(raw)
            step.details["links"] = links
            urls = [link["url"] for link in links]
            expected = [f"https://example.org/{name}" for name in ("alpha", "beta", "gamma")]
            run.confirm(f"собрано {len(links)}: {urls}", ok=urls == expected)
            if urls != expected:
                step.error = f"ожидались {expected}, получены {urls}"
                return
            result_file.write_text(json.dumps(links, ensure_ascii=False, indent=1), encoding="utf-8")
            step.details["file"] = str(result_file)

        with run.step("записать результат в файл", expect="файл содержит три ссылки",
                      method="чтение записанного файла") as step:
            saved = json.loads(result_file.read_text(encoding="utf-8"))
            titles = [item["title"] for item in saved]
            step.details["titles"] = titles
            run.confirm(f"в файле {len(saved)} ссылок, заголовки: {titles[:2]}...",
                        ok=len(saved) == 3 and all("источник" in title for title in titles))
            if len(saved) != 3:
                step.error = "в файле не три ссылки"

        with run.step("закрыть браузер", expect="наш процесс завершён",
                      method="CDP: Browser.close + проверка процесса") as step:
            try:
                browser_ws = Cdp(cdp.port)
                browser_ws.connect(cdp.endpoint("/json/version")["webSocketDebuggerUrl"])
                browser_ws.call("Browser.close")
                browser_ws.close()
            except Exception as exc:  # noqa: BLE001 — закрытие могло уже случиться
                step.details["close_note"] = str(exc)[:120]
            cdp.close()
            deadline = time.time() + 10
            while time.time() < deadline and process.poll() is None:
                time.sleep(0.3)
            step.details["returncode"] = process.poll()
            run.confirm(f"процесс завершён: {process.poll() is not None}",
                        ok=process.poll() is not None)
    finally:
        if cdp is not None:
            cdp.close()
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        # После закрытия добиваем свои дочерние процессы, иначе следующий прогон
        # не сможет занять профиль.
        kill_our_browsers(profile)
