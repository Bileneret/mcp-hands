"""Живой пробник: управляема ли Opera GX по CDP (основа «живого слоя»).

Запускает ОТДЕЛЬНЫЙ экземпляр Opera GX с нашим профилем и отладочным портом
(браузер пользователя не затрагивается), открывает живой YouTube, читает
заголовок/состояние страницы через протокол и закрывает всё деревом процессов.
Ничего не нажимает в браузере пользователя; кликов нет вовсе.

Запуск: .venv\\Scripts\\python.exe -u spike\\live_opera_probe.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcp_hands.scenarios.browser_links import Cdp, free_port  # noqa: E402

OPERA_PATHS = [
    Path.home() / "AppData/Local/Programs/Opera GX/opera.exe",
    Path.home() / "AppData/Local/Programs/Opera/opera.exe",
    Path(r"C:\Program Files\Opera\opera.exe"),
]
PROFILE = ROOT / "logs" / "live_probe" / "opera_profile"


def find_opera() -> Path | None:
    for candidate in OPERA_PATHS:
        if candidate.exists():
            return candidate
    return None


def probe(headless: bool) -> dict:
    opera = find_opera()
    if opera is None:
        return {"ok": False, "reason": "opera.exe не найден"}
    PROFILE.mkdir(parents=True, exist_ok=True)
    port = free_port()
    args = [str(opera), f"--remote-debugging-port={port}",
            f"--user-data-dir={PROFILE}", "--remote-allow-origins=*",
            "--no-first-run", "--no-default-browser-check", "about:blank"]
    if headless:
        args.insert(1, "--headless=new")
    report: dict = {"ok": False, "headless": headless, "port": port}
    started = time.perf_counter()
    process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cdp = Cdp(port, timeout_s=40.0)
    try:
        version = cdp.wait_ready()
        report["browser"] = version.get("Browser", "")
        report["start_s"] = round(time.perf_counter() - started, 1)
        tab = cdp.open_page("https://www.youtube.com", attempts=6)
        report["tab_id"] = tab.get("id", "")[:12]
        cdp.connect(tab["webSocketDebuggerUrl"])
        cdp.call("Page.enable")
        time.sleep(4.0)  # живой сайт: дать загрузиться
        info = cdp.call("Runtime.evaluate", expression=(
            "({title: document.title, url: location.href, "
            "ready: document.readyState, "
            "links: document.querySelectorAll('a').length, "
            "videos: document.querySelectorAll('video').length, "
            "inputs: document.querySelectorAll('input').length})"),
            returnByValue=True)["result"]["value"]
        report.update(info)
        report["ok"] = bool(info.get("title")) and "youtube" in info.get("url", "")
    except Exception as exc:  # noqa: BLE001 — проба и есть проверка
        report["reason"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    finally:
        cdp.close()
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                       capture_output=True, timeout=20)
    return report


def main() -> int:
    for headless in (True, False):
        print(f"— проба (headless={headless}) …")
        result = probe(headless)
        print(json.dumps(result, ensure_ascii=False, indent=1)[:900])
        if result.get("ok"):
            print("ИТОГ: Opera GX управляема по CDP.")
            return 0
    print("ИТОГ: проба не удалась (см. причины выше).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
