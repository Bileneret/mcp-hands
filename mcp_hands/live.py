"""Живой слой (этап «живые сценарии»): управление настоящим браузером по CDP.

Два режима профиля (решение владельца):
- `work` — отдельный профиль в каталоге прогона: тесты, автоматика, ничего
  личного (по умолчанию);
- `user` — ЖИВАЯ Opera владельца со входами: подключаемся к экземпляру,
  запущенному через `tools/opera_debug_launcher.py` (отладочный порт). Мы
  НЕ перезапускаем и не закрываем браузер пользователя: нет порта — понятный
  отказ с инструкцией.

Управление — протоколом (навигация, поиск, DOM-клик, видео, полноэкранный
режим, скриншот), НЕ кликами вслепую. Каждая операция возвращает факты:
`video.paused`, `document.fullscreenElement`, число результатов поиска.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from pathlib import Path

from .scenarios.browser_links import Cdp, CdpTransportError, free_port

ROOT = Path(__file__).resolve().parents[1]
WORK_PROFILE = ROOT / "logs" / "live" / "opera_profile"
USER_PORT_FILE = ROOT / "logs" / "live" / "user_port.txt"
DEFAULT_USER_PORT = 9222

OPERA_PATHS = [
    Path.home() / "AppData/Local/Programs/Opera GX/opera.exe",
    Path.home() / "AppData/Local/Programs/Opera/opera.exe",
    Path(r"C:\Program Files\Opera\opera.exe"),
]


def opera_path() -> Path | None:
    return next((candidate for candidate in OPERA_PATHS if candidate.exists()), None)


def kill_our_opera(profile: Path) -> int:
    """Снимает ТОЛЬКО наши экземпляры Opera — отбор строго по пути профиля в
    командной строке. Браузер владельца (личный профиль) не трогается никогда."""
    import subprocess

    command = ("Get-CimInstance Win32_Process -Filter \"Name='opera.exe'\" | "
               f"Where-Object {{ $_.CommandLine -like '*{profile}*' }} | "
               "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction "
               "SilentlyContinue; $_.ProcessId }")
    try:
        completed = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                                   capture_output=True, timeout=30)
        return len([line for line in completed.stdout.decode("utf-8", "replace").split()
                    if line.isdigit()])
    except Exception:  # noqa: BLE001 — уборка не должна ломать запуск
        return 0


class LiveBrowser:
    """Настоящий браузер под CDP: рабочий профиль (свой запуск) или пользовательский (подключение)."""

    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self.cdp: Cdp | None = None
        self.port = 0
        self.mode = ""
        self.tab: dict = {}

    # --- жизненный цикл -------------------------------------------------

    def start_work(self, headless: bool = True) -> dict:
        """Свой экземпляр с отдельным профилем — для тестов и автоматики.

        Перед запуском снимаются ПРОШЛЫЕ наши экземпляры (по пути профиля):
        живая сессия живёт до явного stop, а занятый профиль не даёт поднять
        новый процесс (Opera просто передаёт управление старому) — без этой
        уборки второй запуск ловил «порт не поднялся».
        """
        opera = opera_path()
        if opera is None:
            raise RuntimeError("opera.exe не найден (Opera GX / Opera)")
        killed = kill_our_opera(WORK_PROFILE)
        if killed:
            time.sleep(1.5)
        WORK_PROFILE.mkdir(parents=True, exist_ok=True)
        port = free_port()
        args = [str(opera), f"--remote-debugging-port={port}",
                f"--user-data-dir={WORK_PROFILE}", "--remote-allow-origins=*",
                "--no-first-run", "--no-default-browser-check", "about:blank"]
        if headless:
            args.insert(1, "--headless=new")
        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
        self.cdp = Cdp(port, timeout_s=40.0)
        version = self.cdp.wait_ready()
        self.port, self.mode = port, "work"
        return {"mode": "work", "headless": headless, "port": port,
                "browser": version.get("Browser", "")}

    def connect_user(self) -> dict:
        """Подключается к ЖИВОЙ Opera владельца (запущенной с отладочным портом)."""
        port = DEFAULT_USER_PORT
        if USER_PORT_FILE.exists():
            try:
                port = int(USER_PORT_FILE.read_text(encoding="utf-8").strip())
            except ValueError:
                pass
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version",
                                        timeout=3) as response:
                version = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 — порт закрыт: браузер не в отладочном режиме
            return {"ok": False, "reason": (
                f"живая Opera недоступна на порту {port}: {exc}. Запусти её через "
                f"tools/opera_debug_launcher.py (порт {DEFAULT_USER_PORT}); твоя "
                "текущая сессия не закрывается молча — лончер сам проверит состояние")}
        self.cdp = Cdp(port, timeout_s=20.0)
        self.port, self.mode = port, "user"
        return {"ok": True, "mode": "user", "port": port,
                "browser": version.get("Browser", "")}

    def stop(self) -> dict:
        """Закрывает ТОЛЬКО наш экземпляр; в user-режиме — просто отключается."""
        if self.cdp is not None:
            self.cdp.close()
            self.cdp = None
        if self.mode == "user":
            self.mode = ""
            return {"stopped": False, "detached": True,
                    "note": "браузер владельца не закрывается"}
        killed = 0
        if self.process is not None:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.process.pid)],
                               capture_output=True, timeout=20)
                killed = 1
            except Exception:  # noqa: BLE001
                pass
            try:
                self.process.wait(timeout=10)
            except Exception:  # noqa: BLE001
                pass
            self.process = None
        self.mode = ""
        return {"stopped": True, "killed": bool(killed)}
    # --- операции -------------------------------------------------------

    def _cdp(self) -> Cdp:
        """Клиент сессии или внятный отказ: «нет соединения» — это тоже связь."""
        if self.cdp is None:
            raise CdpTransportError("обрыв связи: живая сессия не подключена "
                                    "(live_browser start/connect)")
        return self.cdp

    def _endpoint(self, path: str = "/json/list") -> list:
        """Список вкладок отладочного порта; мёртвый порт → ошибка связи."""
        try:
            return self._cdp().endpoint(path)
        except Exception as exc:  # noqa: BLE001 — порт закрылся вместе с браузером
            raise CdpTransportError(
                f"обрыв связи: отладочный порт {self.port} не отвечает "
                f"({type(exc).__name__}: {exc})") from exc

    def _open_page(self, url: str) -> dict:
        try:
            return self._cdp().open_page(url)
        except Exception as exc:  # noqa: BLE001 — вкладку открыть не удалось: порт мёртв
            raise CdpTransportError(
                f"обрыв связи: не удалось открыть вкладку на порту {self.port} "
                f"({type(exc).__name__}: {exc})") from exc

    def _connect_tab(self, tab: dict) -> None:
        try:
            self._cdp().connect(tab["webSocketDebuggerUrl"])
        except Exception as exc:  # noqa: BLE001 — вкладка не принимает соединение
            raise CdpTransportError(
                f"обрыв связи: вкладка не принимает подключение "
                f"({type(exc).__name__}: {exc})") from exc

    def _call(self, method: str, **params) -> dict:
        """Вызов CDP с самолечением: разрыв связи → перепривязка к вкладке → один повтор.

        Живая сцена длится минуты, а вкладка (или целый браузер) в это время может
        закрыться: в логе владельца это давало «WinError 10053» и мёртвую сцену.
        Теперь слой сначала пробует восстановиться и лишь потом честно отказывает;
        ЛЮБОЙ путь отказа связи приводит к CdpTransportError с маркером «обрыв связи»
        — по нему ассистент понимает, что нужно перезапустить браузер.
        """
        try:
            return self._require().call(method, **params)
        except CdpTransportError as first:
            try:
                self._reattach()
                return self._require().call(method, **params)
            except Exception as exc:  # noqa: BLE001 — второй отказ уже окончательный
                raise CdpTransportError(
                    f"обрыв связи с браузером: {first}; повтор не удался: "
                    f"{type(exc).__name__}: {exc}") from exc

    def _reattach(self) -> None:
        """Возвращает рабочую точку после разрыва: та же вкладка, иначе — новая."""
        previous_id = (self.tab or {}).get("id")
        if self.cdp is not None:
            self.cdp.close()
            self.tab = {}
            if previous_id:
                try:
                    for tab in self.cdp.endpoint("/json/list"):
                        if tab.get("id") == previous_id and tab.get("type") == "page":
                            self.tab = tab
                            self.cdp.connect(tab["webSocketDebuggerUrl"])
                            self.cdp.call("Page.enable")
                            return
                except Exception:  # noqa: BLE001 — вкладки уже нет: откроем свою
                    self.tab = {}
        if self.mode:
            self._require()

    def _require(self) -> Cdp:
        cdp = self._cdp()
        if not self.tab:
            # рабочая вкладка ещё не выбрана: у себя берём первую, в браузере
            # ВЛАДЕЛЬЦА открываем СВОЮ (его вкладки не трогаем никогда)
            if self.mode == "user":
                tab = self._open_page("about:blank")
                self.tab = tab
                self._connect_tab(tab)
                self._call("Page.enable")
            else:
                self.ensure_tab()
        return cdp

    def ensure_tab(self) -> dict:
        """Первая вкладка типа page (или единственная) как рабочая точка."""
        cdp = self._cdp()
        pages = [tab for tab in self._endpoint("/json/list")
                 if tab.get("type") == "page"]
        if not pages:
            raise CdpTransportError("обрыв связи: в браузере нет открытых вкладок")
        self.tab = pages[0]
        self._connect_tab(self.tab)
        cdp.call("Page.enable")
        return {"tab": self.tab.get("id", "")[:12], "url": self.tab.get("url", "")}

    def evaluate(self, expression: str):
        result = self._call("Runtime.evaluate", expression=expression, returnByValue=True)
        return result.get("result", {}).get("value")

    def status(self) -> dict:
        """Факты о живой сессии для отчёта и для вопроса «видишь браузер?»:
        режим, порт и что открыто в рабочей вкладке. Пользовательские вкладки
        не трогаются: без нашей вкладки просто сообщаем, что её ещё нет."""
        facts: dict = {"mode": self.mode, "port": self.port,
                       "tab_url": (self.tab or {}).get("url", "")}
        if self.mode and self.tab:
            try:
                page = self.page_info() or {}
                facts.update({"url": page.get("url", ""), "title": page.get("title", ""),
                              "videos": page.get("videos", 0),
                              "search_inputs": page.get("search_inputs", [])})
            except Exception as exc:  # noqa: BLE001 — состояние вкладки может быть любым
                facts["note"] = f"вкладка не отвечает: {type(exc).__name__}"
        elif self.mode:
            facts["note"] = "рабочая вкладка ещё не открыта"
        return facts

    def wait_for(self, expression: str, timeout_s: float = 15.0, step_s: float = 0.5) -> bool:
        """Ждёт, пока выражение станет истинным: сетевые страницы ленивы."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                if self.evaluate(expression):
                    return True
            except Exception:  # noqa: BLE001 — страница могла перезагружаться
                pass
            time.sleep(step_s)
        return False

    def navigate(self, url: str, load_timeout_s: float = 25.0) -> dict:
        if self.mode == "user":
            # В браузере ВЛАДЕЛЬЦА никогда не навигируем в его вкладках:
            # открываем СВОЮ новую вкладку и работаем в ней — его страницы целы.
            tab = self._open_page(url)
            self.tab = tab
            self._connect_tab(tab)
            self._call("Page.enable")
            loaded = self.wait_for("document.readyState === 'complete'", load_timeout_s)
            return {"url": self.evaluate("location.href"),
                    "title": self.evaluate("document.title") or "",
                    "ready": self.evaluate("document.readyState"), "loaded": loaded,
                    "tab": "новая (своя)"}
        if not self.tab:
            self.ensure_tab()
        self._call("Page.navigate", url=url)
        ready = self.wait_for("document.readyState === 'complete'", load_timeout_s)
        return {"url": self.evaluate("location.href"),
                "title": self.evaluate("document.title") or "",
                "ready": self.evaluate("document.readyState"), "loaded": ready}

    def page_info(self) -> dict:
        return self.evaluate(
            "({url: location.href, title: document.title, ready: document.readyState,"
            " links: document.querySelectorAll('a').length,"
            " videos: document.querySelectorAll('video').length,"
            " search_inputs: Array.from(document.querySelectorAll("
            "  'input[type=search], input[name=q], input[name=search_query], #search'))"
            "  .map(function(i){return i.name || i.id || i.type;})})")

    SEARCH_JS = """
(() => {
  const selectors = [
    'input#search', 'input[name=search_query]', 'input[type=search]',
    'input[name=q]', 'textarea[name=search_query]',
  ];
  let box = null;
  for (const sel of selectors) { box = document.querySelector(sel); if (box) break; }
  if (!box) return {ok: false, reason: 'не нашлось поля поиска'};
  const value = QUERY_TEXT;
  box.focus();
  const setter = Object.getOwnPropertyDescriptor(
      box instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
                                          : HTMLInputElement.prototype, 'value').set;
  setter.call(box, value);
  box.dispatchEvent(new Event('input', {bubbles: true}));
  const form = box.closest('form');
  const before = location.href;
  if (form) { form.submit(); return {ok: true, how: 'form.submit', before: before}; }
  const event = new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter',
      keyCode: 13, which: 13, bubbles: true});
  box.dispatchEvent(event);
  return {ok: true, how: 'enter', before: before};
})()
"""

    def search(self, query: str, wait_timeout_s: float = 20.0) -> dict:
        """Печатает запрос в поиске текущей страницы и подтверждает выданные результаты."""
        before = self.evaluate("location.href")
        outcome = self.evaluate(self.SEARCH_JS.replace("QUERY_TEXT", json.dumps(query)))
        if not (outcome or {}).get("ok"):
            return {"ok": False, "reason": (outcome or {}).get("reason", "поиск не сработал")}
        changed = self.wait_for(f"location.href !== {json.dumps(before)}", wait_timeout_s)
        settled = self.wait_for(
            "document.readyState === 'complete' && document.querySelectorAll('a').length > 30",
            wait_timeout_s)
        harvest = self.evaluate((
            "(() => { const seen = new Set(); const out = [];"
            "  document.querySelectorAll('a[href]').forEach(a => {"
            "    const text = (a.textContent || '').trim();"
            "    if (text.length > 12 && !seen.has(text) && out.length < 10) {"
            "      seen.add(text); out.push({text: text.slice(0, 90), href: a.href.slice(0, 140)});"
            "    }});"
            "  return {url: location.href, title: document.title, results: out}; })()"))
        return {"ok": bool(harvest and harvest.get("results")),
                "how": outcome.get("how"), "url_changed": changed, "settled": settled,
                "found": len((harvest or {}).get("results", [])),
                "top": (harvest or {}).get("results", [])[:5]}

    CLICK_JS = """
(() => {
  const text = CLICK_TEXT;
  const candidates = Array.from(document.querySelectorAll(
      'a, button, [role=link], [role=button], ytd-video-renderer a#thumbnail, h3 a'));
  const hit = candidates.find(node => (node.textContent || '').trim()
      .toLowerCase().includes(text.toLowerCase()));
  if (!hit) return {ok: false, reason: 'элемент с таким текстом не найден'};
  hit.scrollIntoView({block: 'center'});
  hit.click();
  return {ok: true, tag: hit.tagName.toLowerCase(),
          text: (hit.textContent || '').trim().slice(0, 90), href: (hit.href || '').slice(0, 140)};
})()
"""

    PICK_VIDEO_JS = """
(() => {
  const links = Array.from(document.querySelectorAll(
      "ytd-video-renderer a#thumbnail[href*='/watch'], ytd-video-renderer a#video-title[href*='/watch'], a[href*='/watch?v=']"));
  const good = links.filter(a => !(a.textContent || '').includes('Текущее видео'));
  if (!good.length) return {ok: false, reason: 'нет ссылок-видео в выдаче'};
  const pick = good[0];
  pick.scrollIntoView({block: 'center'});
  pick.click();
  return {ok: true, href: pick.href, text: (pick.textContent || '').trim().slice(0, 80)};
})()
"""

    def open_first_video(self, wait_timeout_s: float = 20.0) -> dict:
        """Открыть первое видео из выдачи: DOM-клик, а если страница не сменилась —
        прямой переход по собранному href (протокол надёжнее «поймать» вёрстку)."""
        picked = self.evaluate(self.PICK_VIDEO_JS)
        if not (picked or {}).get("ok"):
            return {"ok": False, "reason": (picked or {}).get("reason", "выбор не сработал")}
        href = picked.get("href", "")
        on_watch = self.wait_for("location.pathname.startsWith('/watch')", 6.0)
        how = "click"
        if not on_watch and href:
            navigation = self.navigate(href)
            on_watch = bool(navigation.get("loaded"))
            how = "navigate-fallback"
        picked.update({"ok": bool(on_watch), "how": how})
        if not on_watch:
            picked["reason"] = "страница видео не открылась"
        return picked

    def click_text(self, text: str) -> dict:
        """DOM-клик по элементу с заданным текстом (не координатный «слепой» клик)."""
        return self.evaluate(self.CLICK_JS.replace("CLICK_TEXT", json.dumps(text))) or \
            {"ok": False, "reason": "страница не ответила"}

    VIDEO_JS = """
(() => {
  const video = document.querySelector('video');
  if (!video) return {ok: false, reason: 'на странице нет video'};
  const action = VIDEO_ACTION;
  if (action === 'play') { video.play(); }
  if (action === 'pause') { video.pause(); }
  if (action === 'fullscreen') {
    const target = video.closest('ytd-watch-flexy') || video;
    (target.requestFullscreen ? target.requestFullscreen()
                              : Promise.resolve()).catch(() => {});
    Promise.resolve().then(() => video.requestFullscreen && video.requestFullscreen())
                  .catch(() => {});
  }
  if (action === 'exit') { if (document.fullscreenElement) { document.exitFullscreen(); } }
  return {ok: true};
})()
"""

    def press_key(self, key: str, code: str = "", key_code: int = 0) -> dict:
        """Доверенное событие клавиши В СТРАНИЦУ через CDP (не ОС-клавиатура).

        Полноэкранный режим YouTube требует «жеста пользователя»: JS-вызов
        `requestFullscreen()` без него отклоняется. Событие из CDP доставляется
        странице как доверенное, и YouTube сам вызывает API из обработчика.
        """
        cdp = self._require()
        payload = {"type": "keyDown", "key": key, "code": code or key,
                   "windowsVirtualKeyCode": key_code or ord(key.upper()[0]),
                   "nativeVirtualKeyCode": key_code or ord(key.upper()[0])}
        # Без авто-повтора: повторное нажатие переключило бы режим обратно
        # (полный экран). Надёжность даёт постусловие уровня выше — состояние
        # `document.fullscreenElement` проверяется после каждого шага.
        cdp.call("Input.dispatchKeyEvent", **payload)
        cdp.call("Input.dispatchKeyEvent", **{**payload, "type": "keyUp"})
        return {"ok": True, "key": key}

    def video(self, action: str = "status", wait_timeout_s: float = 15.0) -> dict:
        """Статус/управление видео: play, pause, fullscreen, exit; факты после действия."""
        if action != "status":
            outcome = self.evaluate(self.VIDEO_JS.replace("VIDEO_ACTION", json.dumps(action)))
            if not (outcome or {}).get("ok"):
                return {"ok": False, "reason": (outcome or {}).get("reason", "действие не прошло")}
        if action == "play":
            self.wait_for("!!document.querySelector('video') && "
                          "!document.querySelector('video').paused", wait_timeout_s)
        if action == "fullscreen":
            # 1) прямой JS-вызов (может пройти без жеста), 2) доверенная клавиша 'f'
            # внутри страницы — штатный хоткей YouTube для полноэкранного режима.
            if not self.wait_for("!!document.fullscreenElement", 3.0):
                self.press_key("f", code="KeyF", key_code=70)
                self.wait_for("!!document.fullscreenElement", 8.0)
        if action == "exit":
            self.wait_for("!document.fullscreenElement", 5.0)
        status = self.evaluate(
            "(() => { const v = document.querySelector('video');"
            "  return {ok: true, has_video: !!v, paused: v ? v.paused : null,"
            "    current_time: v ? Math.round(v.currentTime * 10) / 10 : null,"
            "    duration: v && isFinite(v.duration) ? Math.round(v.duration) : null,"
            "    ready_state: v ? v.readyState : null,"
            "    fullscreen: !!document.fullscreenElement,"
            "    title: document.title}; })()") or {}
        return status

    def screenshot(self, path: str, full_page: bool = False) -> dict:
        """Скриншот страницы через CDP (работает и в headless, и в user-режиме)."""
        self._require()  # привязка вкладки до захвата: свой отказ, а не CDP-ошибка
        params = {"format": "png", "captureBeyondViewport": bool(full_page)}
        result = self._call("Page.captureScreenshot", **params)
        import base64

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = base64.b64decode(result.get("data", ""))
        target.write_bytes(data)
        written = target.stat().st_size if target.exists() else 0
        # Пустая страница (about:blank) весит меньше килобайта — это всё равно
        # состоявшийся снимок; отказом считается только отсутствие данных, и он
        # ОБЯЗАН быть с причиной (иначе ассистент говорит «неизвестная причина»).
        outcome = {"file": str(target), "bytes": written, "ok": written > 0}
        if not written:
            outcome["reason"] = ("кадр не получен: вкладка не отрисована "
                                 "(CDP вернул пустые данные)")
        return outcome
