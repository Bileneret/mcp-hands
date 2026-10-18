"""Браузерные инструменты (колонна C, этап 5-пре): сессии вкладок и сбор со страниц.

Принципы (решения контура):
1. Работаем ТОЛЬКО со своим headless-браузером и своим профилем в каталоге
   прогона (`--user-data-dir`); процессы с нашим профилем снимаются уборкой
   строго по пути профиля — браузер пользователя не затрагивается никогда.
2. Внешний веб в бенчмарке не используется: задачи идут на локальных страницах
   (стенды), повторяемость важнее эффектности.
3. Постусловия подтверждаются фактически: список вкладок/страница перечитываются
   ПОСЛЕ действия (для закрытия — что осталось ровно сохраняемое), файлы — на диске.
"""

from __future__ import annotations

import csv
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .scenarios.browser_links import Cdp, edge_path, free_port, kill_our_browsers, launch_browser


def domain_of(url: str) -> str:
    """Домен ссылки: для группировки вкладок и источников."""
    parsed = urllib.parse.urlparse(url)
    return parsed.netloc or "(локальный файл)"


class BrowserSession:
    """Свой headless-браузер с CDP: открытие вкладок, чтение списка, закрытие."""

    def __init__(self, profile: Path, browser: str | None = None) -> None:
        self.profile = Path(profile)
        self.browser = browser or edge_path()
        self.process = None
        self.cdp: Cdp | None = None
        self.port = 0

    def start(self) -> dict:
        if not self.browser:
            raise RuntimeError("не найден Edge (msedge.exe) для браузерных инструментов")
        self.profile.mkdir(parents=True, exist_ok=True)
        self.process, self.cdp = launch_browser(self.browser, self.profile)
        self.port = self.cdp.port
        version = self.cdp.wait_ready()
        return {"browser": version.get("Browser", ""), "port": self.port}

    def tabs(self) -> list[dict]:
        """Все вкладки с отладочного порта: id, url, title, type."""
        if self.cdp is None:
            raise RuntimeError("браузер не поднят")
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list",
                                    timeout=5) as response:
            raw = json.loads(response.read().decode("utf-8"))
        return [{"id": item.get("id", ""), "url": item.get("url", ""),
                 "title": item.get("title", ""), "type": item.get("type", "")}
                for item in raw]

    def open(self, url: str) -> dict:
        if self.cdp is None:
            raise RuntimeError("браузер не поднят")
        return self.cdp.open_page(url)

    def close_tab(self, tab_id: str) -> bool:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/json/close/{tab_id}", timeout=5):
                return True
        except Exception:  # noqa: BLE001 — закрытие уже закрытой вкладки не ошибка
            return False

    def stop(self) -> int:
        """Останавливает ТОЛЬКО наш браузер: дерево процессов + уборка по профилю.

        Дочерние процессы Chromium (рендерер, GPU, crashpad) не несут в командной
        строке `--user-data-dir`, поэтому фильтр по профилю их не находит —
        родителя снимаем деревом (`taskkill /T`). Чужие браузеры не затрагиваются:
        цель — pid НАШЕГО процесса.
        """
        import subprocess

        self.cdp_close()
        if self.process is not None:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.process.pid)],
                               capture_output=True, timeout=20)
            except Exception:  # noqa: BLE001 — дерево могло умереть само
                pass
            try:
                self.process.wait(timeout=10)
            except Exception:  # noqa: BLE001
                pass
        # добивка: процессы, которые всё же несут наш профиль в командной строке
        return kill_our_browsers(self.profile)

    def cdp_close(self) -> None:
        if self.cdp is not None:
            self.cdp.close()
            self.cdp = None


ARTICLE_JS = """
(() => {
  const node = document.querySelector('article, main') || document.body;
  return {
    title: (document.querySelector('h1')?.textContent || document.title || '').trim(),
    url: location.href,
    text: (node.innerText || '').trim().slice(0, 20000),
  };
})()
"""


def extract_article(url: str, profile_root: str | Path | None = None) -> dict:
    """Забирает заголовок и текст статьи со страницы (article/main, иначе body).

    Возвращает {"ok", "title", "url", "text"}; пустая статья — честный отказ.
    """
    profile_root = Path(profile_root) if profile_root else \
        Path(__file__).resolve().parents[1] / "logs" / "browser_profile"
    session = BrowserSession(profile_root)
    report: dict = {"ok": False, "url": url}
    try:
        session.start()
        tab = session.open(url)
        session.cdp.connect(tab["webSocketDebuggerUrl"])
        session.cdp.call("Page.enable")
        session.cdp.call("Page.navigate", url=url)
        time.sleep(1.0)
        value = session.cdp.call("Runtime.evaluate", expression=ARTICLE_JS,
                                 returnByValue=True)["result"]["value"]
        report.update(value)
        report["ok"] = bool((value.get("text") or "").strip())
        if not report["ok"]:
            report["reason"] = "на странице нет текста статьи"
    except Exception as exc:  # noqa: BLE001
        report["reason"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    finally:
        report["cleanup_killed"] = session.stop()
    return report


VIDEO_JS_SET_RATE = """
(() => {
  const player = document.querySelector('video#player');
  if (!player) return {error: 'нет video#player'};
  const before = player.playbackRate;
  player.playbackRate = RATE_VALUE;
  return {before: before, after: player.playbackRate};
})()
"""

VIDEO_JS_READ = """
(() => {
  const player = document.querySelector('video#player');
  const cues = Array.from(document.querySelectorAll('#transcript .cue')).map(cue => ({
    start: (cue.getAttribute('data-start') || '').trim(),
    end: (cue.getAttribute('data-end') || '').trim(),
    text: (cue.textContent || '').trim(),
  }));
  return {
    title: (document.querySelector('h1')?.textContent || document.title || '').trim(),
    duration: player ? (player.getAttribute('data-duration') || '').trim() : '',
    rate: player ? player.playbackRate : null,
    cues: cues,
  };
})()
"""


def video_capture(url: str, output_json: str, output_notes: str,
                  playback_rate: float = 1.5,
                  profile_root: str | Path | None = None) -> dict:
    """Задача b07 на локальном медиа-стенде: плеер, скорость, таймкоды → заметки.

    Контракт страницы: `video#player` (duration в `data-duration`), реплики в
    `#transcript .cue` с `data-start`/`data-end`, заголовок в `h1`.
    Инструмент: выставляет `playbackRate`, читает реплики, пишет JSON-отчёт и
    заметки с таймкодами. Постусловие: заметки содержат столько же строк-таймкодов,
    сколько реплик на странице; фактическая скорость на странице == заказанной
    (перечитывание ПОСЛЕ установки).
    """
    profile_root = Path(profile_root) if profile_root else \
        Path(output_json).resolve().parent / "browser_profile"
    session = BrowserSession(profile_root)
    report: dict = {"ok": False, "url": url, "rate_requested": playback_rate}
    try:
        session.start()
        tab = session.open(url)
        session.cdp.connect(tab["webSocketDebuggerUrl"])
        session.cdp.call("Page.enable")
        session.cdp.call("Page.navigate", url=url)
        time.sleep(1.0)
        rate_result = session.cdp.call(
            "Runtime.evaluate",
            expression=VIDEO_JS_SET_RATE.replace("RATE_VALUE", str(float(playback_rate))),
            returnByValue=True)["result"]["value"]
        if rate_result.get("error"):
            report["reason"] = rate_result["error"]
            return report
        report["rate_before"] = rate_result.get("before")
        report["rate_after"] = rate_result.get("after")
        data = session.cdp.call("Runtime.evaluate", expression=VIDEO_JS_READ,
                                returnByValue=True)["result"]["value"]
        cues = [cue for cue in data.get("cues", []) if cue.get("start")]
        if not cues:
            report["reason"] = "на странице нет реплик с таймкодами (#transcript .cue)"
            return report
        report.update({"title": data.get("title", ""), "duration": data.get("duration", ""),
                       "cues": len(cues)})
        json_target = Path(output_json)
        json_target.parent.mkdir(parents=True, exist_ok=True)
        json_target.write_text(json.dumps(
            {"title": data.get("title", ""), "duration": data.get("duration", ""),
             "playback_rate": report["rate_after"], "cues": cues},
            ensure_ascii=False, indent=1), encoding="utf-8")
        notes_target = Path(output_notes)
        note_lines = [f"# {data.get('title', '')}", f"Длительность: {data.get('duration', '')}",
                      f"Скорость просмотра: {report['rate_after']}x", "",
                      "## Таймкоды"]
        note_lines += [f"- [{cue['start']}–{cue['end']}] {cue['text']}" for cue in cues]
        notes_target.write_text("\n".join(note_lines) + "\n", encoding="utf-8")
        # постусловия: заметки содержат каждый таймкод; скорость на странице фактическая
        notes_text = notes_target.read_text(encoding="utf-8")
        missing = [cue["start"] for cue in cues if cue["start"] not in notes_text]
        rate_ok = abs(float(report["rate_after"] or 0) - float(playback_rate)) < 1e-6
        report["cues_in_notes"] = len(cues) - len(missing)
        report["notes_file"] = str(notes_target)
        report["json_file"] = str(json_target)
        report["ok"] = (not missing and rate_ok and json_target.is_file()
                        and notes_target.is_file())
        if missing:
            report["reason"] = f"в заметках нет таймкодов: {missing[:3]}"
        elif not rate_ok:
            report["reason"] = f"скорость на странице {report['rate_after']}, заказано {playback_rate}"
    except Exception as exc:  # noqa: BLE001
        report["reason"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    finally:
        report["cleanup_killed"] = session.stop()
    return report


def tabs_audit(urls: list[str], output: str, keep: list[str] | None = None,
               close_extras: bool = True, profile_root: str | Path | None = None) -> dict:
    """Разбор сессии вкладок: открыть заданные, сгруппировать по домену, сохранить
    отчёт, при `close_extras` — закрыть всё, кроме `keep` (по умолчанию keep = уникальные URL).

    Постусловие (проверяет инструмент): файл-отчёт на диске; после закрытия на порту
    остались ровно вкладки из keep (перечитывание списка ПОСЛЕ действия).
    """
    if not urls:
        return {"ok": False, "reason": "пустой список urls"}
    keep_set = list(dict.fromkeys(keep if keep else urls))
    profile_root = Path(profile_root) if profile_root else \
        Path(output).resolve().parent / "browser_profile"
    session = BrowserSession(profile_root)
    report: dict = {"ok": False, "opened": [], "groups": {}, "closed": [], "remaining": []}
    try:
        started = session.start()
        report["browser"] = started
        for url in urls:
            tab = session.open(url)
            report["opened"].append({"url": url, "id": tab.get("id", "")})
        time.sleep(0.6)
        # группировка по домену — текущий список после открытий
        for tab in session.tabs():
            if tab["type"] != "page":
                continue
            report["groups"].setdefault(domain_of(tab["url"]), []).append(tab["url"])
        if close_extras:
            kept_urls: set[str] = set()
            for tab in session.tabs():
                if tab["type"] != "page":
                    continue
                if tab["url"] in ("about:blank", ""):
                    if session.close_tab(tab["id"]):
                        report["closed"].append("about:blank")
                    continue
                if tab["url"] in keep_set and tab["url"] not in kept_urls:
                    kept_urls.add(tab["url"])  # первый экземпляр сохраняем
                    continue
                if session.close_tab(tab["id"]):
                    report["closed"].append(tab["url"])
            time.sleep(0.5)
        # постусловие: фактический остаток страниц
        remaining = [tab for tab in session.tabs() if tab["type"] == "page"
                     and tab["url"] not in ("about:blank",)]
        report["remaining"] = [tab["url"] for tab in remaining]
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        remaining_ok = (len(report["remaining"]) == len(keep_set)) if close_extras else True
        report["ok"] = target.is_file() and remaining_ok
        if not remaining_ok:
            report["reason"] = (f"после закрытия осталось {report['remaining']}, "
                                f"ожидалось {keep_set}")
    except Exception as exc:  # noqa: BLE001 — авария браузера = отказ с причиной
        report["reason"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    finally:
        report["cleanup_killed"] = session.stop()
    return report


COLLECT_JS = """
(() => {
  const cards = Array.from(document.querySelectorAll('article.source'));
  return cards.map(card => ({
    title: (card.querySelector('a')?.textContent || '').trim(),
    url: card.querySelector('a')?.href || '',
    author: (card.querySelector('.author')?.textContent || '').trim(),
    year: (card.getAttribute('data-year') || '').trim(),
  }));
})()
"""


def collect_sources(url: str, output_csv: str,
                    profile_root: str | Path | None = None) -> dict:
    """Собрать источники со страницы-стенда в CSV (автор, год, ссылка, заголовок).

    Контракт страницы: карточки `article.source` (ссылка `a[href]`, автор `.author`,
    год в `data-year`). Постусловие: CSV на диске; повторный подсчёт карточек
    в DOM ПОСЛЕ записи совпадает с числом строк CSV.
    """
    profile_root = Path(profile_root) if profile_root else \
        Path(output_csv).resolve().parent / "browser_profile"
    session = BrowserSession(profile_root)
    report: dict = {"ok": False, "url": url, "rows": 0}
    try:
        report["browser"] = session.start()
        tab = session.open(url)
        session.cdp.connect(tab["webSocketDebuggerUrl"])
        session.cdp.call("Page.enable")
        session.cdp.call("Page.navigate", url=url)
        time.sleep(1.0)  # ожидание разбора локальной страницы
        value = session.cdp.call("Runtime.evaluate", expression=COLLECT_JS,
                                 returnByValue=True)["result"]["value"]
        rows = [row for row in value if row.get("url")]
        if not rows:
            report["reason"] = "на странице не нашлось карточек article.source"
        else:
            target = Path(output_csv)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle, delimiter=";")
                writer.writerow(["автор", "год", "ссылка", "заголовок"])
                for row in sorted(rows, key=lambda item: item["author"]):
                    writer.writerow([row["author"], row["year"], row["url"], row["title"]])
            # постусловие: DOM ПОСЛЕ записи даёт столько же карточек
            check = session.cdp.call("Runtime.evaluate", expression=COLLECT_JS,
                                     returnByValue=True)["result"]["value"]
            report["rows"] = len(rows)
            report["dom_after"] = len(check)
            report["file"] = str(target)
            report["ok"] = target.is_file() and len(check) == len(rows)
            if not report["ok"]:
                report["reason"] = (f"DOM после записи даёт {len(check)} карточек, "
                                    f"в CSV {len(rows)}")
    except Exception as exc:  # noqa: BLE001
        report["reason"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    finally:
        report["cleanup_killed"] = session.stop()
    return report
