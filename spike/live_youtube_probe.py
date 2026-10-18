"""Проба живого слоя: YouTube поиск → видео → play → fullscreen (work-профиль).

Запуск: .venv\\Scripts\\python.exe -u spike\\live_youtube_probe.py [запрос]
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcp_hands.live import LiveBrowser  # noqa: E402


def main() -> int:
    query = sys.argv[1] if len(sys.argv) > 1 else "новости сегодня"
    browser = LiveBrowser()
    try:
        print("1. запуск:", json.dumps(browser.start_work(headless=True), ensure_ascii=False))
        print("2. вкладка:", json.dumps(browser.ensure_tab(), ensure_ascii=False))
        nav = browser.navigate("https://www.youtube.com")
        print("3. навигация:", json.dumps(nav, ensure_ascii=False)[:200])
        time.sleep(2.0)
        print("4. страница:", json.dumps(browser.page_info(), ensure_ascii=False)[:240])
        found = browser.search(query)
        print("5. поиск:", json.dumps(found, ensure_ascii=False)[:400])
        top = (found.get("top") or [])
        if not top:
            print("   нет результатов — стоп")
            return 1
        clicked = browser.click_text(top[0]["text"][:40])
        print("6. клик по первому результату:", json.dumps(clicked, ensure_ascii=False)[:220])
        appeared = browser.wait_for("!!document.querySelector('video')", 20.0)
        print("7. видео появилось:", appeared)
        status = browser.video("status")
        print("8. статус до play:", json.dumps(status, ensure_ascii=False))
        if status.get("paused"):
            print("9. play:", json.dumps(browser.video("play"), ensure_ascii=False))
        full = browser.video("fullscreen")
        print("10. fullscreen:", json.dumps(full, ensure_ascii=False))
        shot = browser.screenshot(str(ROOT / "logs" / "live" / "youtube_probe.png"))
        print("11. скриншот:", json.dumps(shot, ensure_ascii=False))
        return 0 if (full.get("fullscreen") or full.get("has_video")) else 1
    finally:
        print("12. закрытие:", json.dumps(browser.stop(), ensure_ascii=False))


if __name__ == "__main__":
    raise SystemExit(main())
