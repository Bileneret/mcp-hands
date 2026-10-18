"""Сводка метрик этапа 5: Markdown-матрица из артефактов сьюта и памяти.

Источники (все — записанные факты, ничего не выдумывается):
1. Scripted-серия (`suite_*.json`, mode = scripted): время по задачам avg/min/max.
2. LIVE-прогон (`suite_*.json`, mode = llm): время сети и токены модели.
3. Память (`agent_memory.db`): автономность, инциденты.
4. `docs/HUMAN_BASELINE.md`: результат ручного хронометража владельца —
   если поля ещё пусты, сводка честно пишет «ожидает замера», а не оценку.

Запуск: python -m mcp_hands.benchmark.report [--out <путь.md>]
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parents[2] / "logs" / "benchmark"
DB = Path(__file__).resolve().parents[2] / "logs" / "agent_memory.db"
BASELINE = Path(__file__).resolve().parents[2] / "docs" / "HUMAN_BASELINE.md"


def _latest(mode: str, bench_dir: Path = BENCH_DIR) -> Path | None:
    candidates = []
    for path in bench_dir.glob("suite_*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(data.get("mode", "")).startswith(mode):
            candidates.append((path.stat().st_mtime, path))
    return sorted(candidates)[-1][1] if candidates else None


def _human_total_seconds(baseline: Path = BASELINE) -> float | None:
    """Сумма замеров владельца из чек-листа (строки `| b01 | … | 1:45 |`)."""
    if not baseline.exists():
        return None
    total = 0.0
    found = False
    for line in baseline.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and cells[0].startswith("b") and ":" in cells[-1]:
            minutes, _, seconds = cells[-1].partition(":")
            try:
                total += int(minutes) * 60 + int(seconds)
                found = True
            except ValueError:
                continue
    return total if found else None


def build_summary(out_path: Path | None = None, bench_dir: Path = BENCH_DIR,
                  db: Path = DB, baseline: Path = BASELINE) -> Path:
    """Собирает Markdown-сводку; пути инъектируются — генератор тестируем без живой базы."""
    scripted_path = _latest("scripted", bench_dir)
    live_path = _latest("llm", bench_dir)
    scripted = json.loads(scripted_path.read_text(encoding="utf-8")) if scripted_path else {}
    live = json.loads(live_path.read_text(encoding="utf-8")) if live_path else {}

    lines: list[str] = []
    lines.append("# MCP-Hands — итоговая матрица метрик бенчмарка\n")
    lines.append(f"_Сгенерировано: {time.strftime('%d.%m.%Y %H:%M')}_\n")
    lines.append("## Серия задач (--dry-llm, воспроизводимо)\n")
    per_task = scripted.get("per_task", {})
    if per_task:
        rounds = scripted.get("rounds", 1)
        lines.append(f"Кругов: **{rounds}**; задач: **{len(per_task)}**; "
                     f"итог: **{scripted.get('passed')} passed / "
                     f"{scripted.get('failed')} failed**\n")
        lines.append("| Задача | Результат | Среднее, с | Мин, с | Макс, с |")
        lines.append("|---|---|---|---|---|")
        avg_total = 0.0
        for tid in sorted(per_task):
            stat = per_task[tid]
            passed = f"{stat.get('passed', 0)}/{stat.get('runs', 0)}"
            avg = stat.get("avg_ms")
            entry = (f"| {tid} | {passed} | "
                     f"{avg / 1000:.2f} | {stat.get('min_ms', 0) / 1000:.2f} | "
                     f"{stat.get('max_ms', 0) / 1000:.2f} |") if avg else \
                    f"| {tid} | {passed} | — | — | — |"
            lines.append(entry)
            if avg:
                avg_total += avg / 1000
        lines.append(f"| **Итого** | **{scripted.get('passed')}/{len(per_task) * rounds}** | "
                     f"**{avg_total:.1f}** | — | — |\n")
    else:
        lines.append("_Net scripted-серии в logs/benchmark — запусти сьюту._\n")

    lines.append("## Живой контур (River / GLM-5.3-Flash)\n")
    if live:
        llm = live.get("metrics", {}).get("llm", {})
        b03 = live.get("per_task", {}).get("b03", {})
        lines.append(f"- режим: **llm**; задача b03: "
                     f"**{'passed' if live.get('passed') else 'failed'}**\n"
                     f"- общее время прогона: {live.get('total_ms', 0) / 1000:.1f} с "
                     f"(план модели + исполнение)\n"
                     f"- вызовов LLM всего в базе: {llm.get('calls')}, "
                     f"токенов: {llm.get('tokens')}\n")
    else:
        lines.append("_LIVE-прогон ещё не делался (`suite --live --tasks b03`)._\n")

    if db.exists():
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT COUNT(*) AS runs,"
            " SUM(CASE WHEN human_interventions = 0 THEN 1 ELSE 0 END) AS autonomous"
            " FROM runs WHERE status != 'running'").fetchone()
        incidents = [dict(r) for r in conn.execute(
            "SELECT kind, COUNT(*) AS n FROM incidents GROUP BY kind")]
        conn.close()
        rate = (row["autonomous"] / row["runs"]) if row["runs"] else None
        lines.append("## Автономность и инциденты (SQLite)\n")
        lines.append(f"- завершённых прогонов: **{row['runs']}**, без вмешательств: "
                     f"**{row['autonomous']}** (rate = {rate:.2f})\n"
                     if rate is not None else "- прогонов нет\n")
        lines.append(f"- инциденты: {json.dumps(incidents, ensure_ascii=False)}\n")

    human_total = _human_total_seconds(baseline)
    lines.append("## Сравнение с человеком (DESIGN §7)\n")
    if scripted.get("total_ms") and human_total:
        agent_seconds = scripted["total_ms"] / 1000 / max(1, scripted.get("rounds", 1))
        lines.append(f"- агент (один круг десяти задач): ~**{agent_seconds:.0f} с**\n"
                     f"- человек (чек-лист владельца): **{human_total:.0f} с**\n"
                     f"- коэффициент ускорения: **×{human_total / agent_seconds:.1f}**\n")
    else:
        lines.append("- замер человека **ожидает владельца**: методика и чек-лист — "
                     "`docs/HUMAN_BASELINE.md` (заполняются два столбца времени, "
                     "сумма подхватывается этой сводкой автоматически)\n")

    lines.append("## Честные оговорки\n")
    lines.append("- все стенды локальные (страницы, файлы, Office) — внешний веб "
                 "намеренно не используется: повторяемость важнее эффектности;\n"
                 "- время включает работу COM/браузера и подтверждения постусловий, "
                 "но не включает холодный запуск Python;\n"
                 "- ранние `failed` в SQLite — артефакты отладки этапов, сохранены "
                 "как есть (провалы не прячутся).\n")

    target = out_path or (bench_dir / "summary_metrics.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    out = Path(argv[argv.index("--out") + 1]) if "--out" in argv else None
    path = build_summary(out)
    print(f"сводка: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
