"""Лончер user-режима: ЖИВАЯ Opera владельца с отладочным портом.

Запускает Opera GX штатным профилем пользователя (все входы/закладки на месте),
добавляя только отладочный порт 9222, которым пользуется живой слой MCP-Hands.

Правила безопасности:
- если Opera уже запущена — НИЧЕГО не делаем: просим закрыть её и запустить
  через этот лончер (иначе конфликт профиля; рабочие вкладки закрывать за
  пользователя мы не имеем права);
- лончер никогда не убивает и не перезапускает существующий браузер;
- порт записывается в logs/live/user_port.txt — сервер читает его при
  `live_browser(connect_user)`.

Запуск: .venv\\Scripts\\python.exe -u tools\\opera_debug_launcher.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcp_hands.live import DEFAULT_USER_PORT, USER_PORT_FILE, opera_path  # noqa: E402


def opera_running() -> list[dict]:
    command = ("Get-CimInstance Win32_Process -Filter \"Name='opera.exe'\" | "
               "Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress")
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                                capture_output=True, timeout=30)
        text = result.stdout.decode("utf-8", "replace").strip() or "[]"
        data = json.loads(text)
        return data if isinstance(data, list) else [data]
    except Exception:  # noqa: BLE001 — проверка не должна ломать лончер
        return []


def port_alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    opera = opera_path()
    if opera is None:
        print("opera.exe не найден (Opera GX / Opera).")
        return 2

    if port_alive(DEFAULT_USER_PORT):
        USER_PORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        USER_PORT_FILE.write_text(str(DEFAULT_USER_PORT), encoding="utf-8")
        print(f"Отладочный порт {DEFAULT_USER_PORT} уже отвечает — твоя Opera, похоже, "
              "запущена через этот лончер. Можно работать: live_browser(connect_user).")
        return 0

    running = opera_running()
    if running:
        print("Opera уже запущена (без отладочного порта).")
        print("Чтобы живой слой мог ею управлять, закрой Opera и запусти заново "
              "ЭТИМ лончером — твоя сессия/входы сохранятся, откроются те же вкладки "
              "(Opera предложит «восстановить сессию»).")
        return 1

    args = [str(opera), f"--remote-debugging-port={DEFAULT_USER_PORT}",
            "--remote-allow-origins=*"]
    process = subprocess.Popen(args)
    print(f"Opera GX запущена с отладочным портом {DEFAULT_USER_PORT} (профиль твой).")
    for _ in range(20):
        if port_alive(DEFAULT_USER_PORT):
            USER_PORT_FILE.parent.mkdir(parents=True, exist_ok=True)
            USER_PORT_FILE.write_text(str(DEFAULT_USER_PORT), encoding="utf-8")
            print("Порт отвечает. Теперь скажи агенту: "
                  "«подключись к моей Opera и …» (live_browser connect_user).")
            print("Это окно лончера можно закрыть; браузер продолжит работать.")
            time.sleep(1.0)
            return 0
        time.sleep(1.0)
    print("Порт не поднялся: закрываю запущенный экземпляр, чтобы не мешал.")
    process.kill()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
