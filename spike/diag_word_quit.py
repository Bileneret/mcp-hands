"""Диагностика завершения Word: что происходит с процессом после Quit."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_hands.office import WordSession
from mcp_hands.probe import DOMAIN_OPERATION_TIMEOUT_S, ProbeProcess


def main() -> int:
    with ProbeProcess(default_timeout_s=DOMAIN_OPERATION_TIMEOUT_S) as satellite:
        word = WordSession(satellite)
        print("старт:", word.start().describe())
        pid = word.pid
        print("pid:", pid, "| образ:", ProbeProcess.process_identity(pid))
        result = satellite.call("word_quit", save=False, timeout_s=30.0)
        print("word_quit ответ:", result)
        for moment in (0.0, 0.5, 1.0, 3.0, 10.0, 20.0):
            time.sleep(moment if moment else 0)
            identity = ProbeProcess.process_identity(pid)
            print(f"  через {moment:5.1f} с: образ={identity['image']!r} created={identity['created']}")
        print("живые процессы Word:", sorted(ProbeProcess.__dict__.get("_x", [])) or "-")
        from mcp_hands.probe_worker import Worker

        print("список WINWORD:", sorted(Worker._winword_pids()))
        print("в списке наш pid:", pid in Worker._winword_pids())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
