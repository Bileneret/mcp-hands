"""Диагностика запуска Word через сателлит."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_hands.office import WordSession
from mcp_hands.probe import DOMAIN_OPERATION_TIMEOUT_S, ProbeProcess


def main() -> int:
    with ProbeProcess(default_timeout_s=DOMAIN_OPERATION_TIMEOUT_S) as satellite:
        word = WordSession(satellite)
        outcome = word.start()
        print("word_start:", outcome.describe())
        print("pid:", word.pid)
        print("история сателлита:", satellite.stats.describe())
        if outcome.ok:
            print("status:", word.status().describe())
            quit_outcome = word.quit()
            print("word_quit:", quit_outcome.describe())
        else:
            print("файл-реестр существует:", satellite.pid_file.exists())
            if satellite.pid_file.exists():
                print("реестр:", satellite.pid_file.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
