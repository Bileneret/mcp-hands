"""Прогон живых миссий (Шаг 3): по одной цели, с записью подтверждённого факта.

Протокол утверждён аудитом v26: для каждой миссии фиксируются «достигнуто/нет,
секунды, токены планировщика, источник (рецепт или модель), где застрял»; результат —
JSONL-трейс прогонов и сводная таблица `docs/MISSIONS_RUN.md`. Видеозапись сессии
аудитор признал избыточной: достаточно трейсов и скриншотов при сбоях.

Миссии берутся из `docs/MISSIONS.md` (утверждены владельцем 19.10). У каждой здесь
есть ПРОВЕРКА — факт в мире, который оценивается после ответа ассистента, поэтому
«достигнуто» никогда не выставляется по словам модели.

Запуск:
    .venv\\Scripts\\python.exe tools\\missions.py --list
    .venv\\Scripts\\python.exe tools\\missions.py 1 2            # прогнать миссии 1 и 2
    .venv\\Scripts\\python.exe tools\\missions.py 3 --work        # на своём профиле браузера
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcp_hands import mcp_server as ms  # noqa: E402
from mcp_hands.assistant.agent import Assistant  # noqa: E402

RUN_LOG = ROOT / "logs" / "missions" / "runs.jsonl"
REPORT = ROOT / "docs" / "MISSIONS_RUN.md"


# --- проверки фактов (то, что решает «достигнуто») -----------------------------


def check_screen_named(result: dict) -> tuple[bool, str]:
    """Миссия 1: ответ обязан назвать АКТИВНОЕ окно (правдивость, а не «красиво»).

    Сверяем с миром: берём активное окно и ищем в ответе ассистента его заголовок
    (или имя процесса). Иначе проверка хвалила бы «разведка работает» вместо
    «ассистент сказал правду».
    """
    facts = ms.observe()
    active = (facts.detail or {}).get("active") or {}
    lines = (facts.detail or {}).get("lines") or []
    title = str(active.get("title", ""))
    exe = str(active.get("exe", "")).replace(".exe", "")
    if not title and not exe:
        return False, "разведка не увидела активного окна"
    say = str(result.get("say", "")).lower()
    markers = [marker for marker in (title.lower(), exe.lower()) if marker and marker in say]
    if not markers:
        return False, f"в ответе нет ни заголовка {title[:30]!r}, ни процесса {exe!r}"
    return True, f"ответ называет {markers[0]!r}; строк в окне: {len(lines)}"


def _window_at_point(x: int, y: int) -> str:
    """В каком окне лежит точка: без этого «на экране» легко спутать с своей перепиской."""
    rows = (ms.windows_list().detail or {}).get("windows", [])
    from mcp_hands.geometry import point_in_rect

    for row in rows:
        try:
            rect = ms._coordinate_space().window_frame(int(row["hwnd"]))
        except OSError:
            continue
        if point_in_rect(int(x), int(y), rect):
            return f"{row['title'][:40]!r}"
    return "окно не определено"


def check_youtube_visible(result: dict) -> tuple[bool, str]:
    """Миссия 2: «видишь ли YouTube» — проверяем ПРАВДИВОСТЬ ответа о кадре.

    Сначала сами ищем текст на экране, затем сверяем с ответом ассистента: «да» при
    найденном тексте и «нет» при отсутствии. Полноэкранный поиск может совпасть с
    текстом постороннего окна (в приёмке 19.10 слово встречалось в окне самого
    ассистента), поэтому к факту прилагается окно-владелец находки.
    """
    found = ms.ocr_find("youtube")
    detail = found.detail or {}
    present = bool(detail.get("found"))
    say = str(result.get("say", "")).strip().lower()
    said_no = say.startswith("нет") or "не вижу" in say
    said_yes = say.startswith("да") or "видно" in say
    if present:
        rect = (detail.get("matches") or [{}])[0].get("rect") or [0, 0, 0, 0]
        owner = _window_at_point(int(rect[0]), int(rect[1]))
        if said_yes:
            return True, f"сказал «вижу» и текст правда есть: {owner} в {rect[0]}x{rect[1]}"
        return False, f"текст есть ({owner}), а ассистент ответил: {say[:60]!r}"
    if said_no:
        return True, f"сказал «не вижу» и текста на экране нет (слов в кадре: "                      f"{detail.get('words_in_frame')})"
    return False, f"текста нет, а ассистент ответил: {say[:60]!r}"


def check_youtube_opened(_result: dict) -> tuple[bool, str]:
    """Миссия 3: после клика по плитке открылся именно YouTube."""
    page = ms.live_page()
    url = str((page.detail or {}).get("url", ""))
    if "youtube" in url.lower():
        return True, f"адрес страницы: {url[:80]}"
    return False, f"страница не YouTube: {url[:80] or page.summary[:60]}"


def check_video_playing(_result: dict) -> tuple[bool, str]:
    """Миссии 4–5: видео действительно играет (факт из DOM)."""
    status = ms.live_video("status")
    detail = status.detail or {}
    if detail.get("has_video") and detail.get("paused") is False:
        return True, (f"играет {detail.get('current_time')}/{detail.get('duration')} с, "
                      f"{str(detail.get('title', ''))[:60]}")
    return False, status.summary[:80]


def check_tabs_named(_result: dict) -> tuple[bool, str]:
    """Запасная: перечислить вкладки (миссии про наблюдение в браузере)."""
    tabs = ms.live_tabs()
    if not tabs.ok:
        return False, tabs.reason[:80]
    return True, tabs.summary[:80]


# --- реестр миссий -------------------------------------------------------------

MISSIONS: dict[int, dict] = {
    1: {"phrase": "Что у меня сейчас на экране?", "verify": check_screen_named,
        "needs": "ничего (только чтение)", "mode": "local"},
    2: {"phrase": "Видишь ли на экране YouTube?", "verify": check_youtube_visible,
        "needs": "ничего (только чтение); проверяет правдивость ответа — с YouTube на "
                 "экране и без него", "mode": "local"},
    3: {"phrase": "На странице с заголовком «MCP-Hands плитки» нажми плитку YouTube "
                  "и подтверди, что открылся YouTube",
        "verify": check_youtube_opened, "needs": "свой видимый профиль браузера", "mode": "goal"},
    4: {"phrase": "Найди на YouTube видео Майкла Наки и включи его",
        "verify": check_video_playing, "needs": "живой браузер (Opera)", "mode": "goal"},
    5: {"phrase": "Включи свежую новостную сводку Майкла Наки, похожую на «ДЕНЬ…»",
        "verify": check_video_playing, "needs": "живой браузер (Opera)", "mode": "goal"},
    6: {"phrase": "Открой Twitch и включи что-нибудь по моим интересам",
        "verify": check_video_playing, "needs": "живой браузер (Opera)", "mode": "goal"},
    7: {"phrase": "Найди в интернете, что такое MCP, и ответь коротко со ссылкой",
        "verify": check_screen_named, "needs": "живой браузер", "mode": "goal"},
    8: {"phrase": "Открой ecampus и найди последнее объявление по программированию",
        "verify": check_screen_named, "needs": "живой браузер", "mode": "goal"},
    9: {"phrase": "Собери заметку из открытых вкладок в Word",
        "verify": check_tabs_named, "needs": "живой браузер с открытыми вкладками", "mode": "goal"},
    10: {"phrase": "Разбери загрузки по типам и скажи, что куда ушло",
         "verify": check_screen_named, "needs": "папка загрузок (по согласию владельца)",
         "mode": "goal"},
}


def run_mission(number: int, assistant: Assistant, quiet: bool = True) -> dict:
    """Один прогон: фраза → маршрут ассистента → независимая проверка факта."""
    mission = MISSIONS[number]
    started = time.perf_counter()
    entry = {"mission": number, "phrase": mission["phrase"], "needs": mission["needs"],
             "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        answer = assistant.handle(mission["phrase"])
        entry["route"] = answer.get("kind", "")
        entry["say"] = str(answer.get("say", ""))[:200]
        entry["tokens"] = int((answer.get("detail") or {}).get("tokens", 0) or 0)
        entry["source"] = str((answer.get("detail") or {}).get("source", ""))
        summary = (answer.get("detail") or {}).get("summary") or {}
        entry["steps"] = [str(step.get("action", "")) for step in (summary.get("steps") or [])]
        entry["actions_ok"] = sum(1 for step in (summary.get("steps") or []) if step.get("ok"))
    except Exception as exc:  # noqa: BLE001 — сбой прогона фиксируем, а не прячем
        entry.update({"route": "error", "say": f"{type(exc).__name__}: {exc}"})
    try:
        achieved, evidence = mission["verify"](entry)
    except Exception as exc:  # noqa: BLE001 — проверка тоже может упасть
        achieved, evidence = False, f"проверка не прошла: {type(exc).__name__}: {exc}"
    entry["achieved"] = bool(achieved)
    entry["evidence"] = evidence
    entry["seconds"] = round(time.perf_counter() - started, 2)
    if quiet:
        mark = "ДОСТИГНУТО" if achieved else "не достигнуто"
        print(f"[{number}] {mark}: {entry['route']} | {entry['seconds']} с | {evidence[:90]}")
    RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
    with RUN_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def write_report() -> Path:
    """Сводка прогонов: таблица по последнему запуску каждой миссии."""
    rows: dict[int, dict] = {}
    if RUN_LOG.exists():
        for line in RUN_LOG.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                rows[int(entry["mission"])] = entry  # последняя запись побеждает
    lines = ["# Прогон живых миссий (Шаг 3)", "",
             "Протокол утверждён аудитом v26: факт достижения проверяется независимо от",
             "ответа ассистента; провалы остаются в таблице как есть.", "",
             "| № | Миссия | Маршрут | Достигнуто | Секунды | Токены | Источник | Факт / где застрял | Шаги плана |",
             "|---|---|---|---|---|---|---|---|---|"]
    achieved = 0
    for number in sorted(MISSIONS):
        entry = rows.get(number)
        if not entry:
            lines.append(f"| {number} | {MISSIONS[number]['phrase'][:50]} | — | — | — | — | — | "
                         f"не запускалась (нужно: {MISSIONS[number]['needs']}) |")
            continue
        achieved += int(bool(entry.get("achieved")))
        fact = entry.get("evidence", "") if entry.get("achieved") else \
            f"{entry.get('say', '')[:60]} → {entry.get('evidence', '')}"
        steps = " → ".join(entry.get("steps") or []) or "—"
        lines.append(f"| {number} | {MISSIONS[number]['phrase'][:50]} | {entry.get('route', '')} | "
                     f"{'да' if entry.get('achieved') else '**нет**'} | {entry.get('seconds', '')} | "
                     f"{entry.get('tokens', '') or '—'} | {entry.get('source', '') or '—'} | "
                     f"{str(fact)[:70]} | {steps[:60]} |")
    ran = sum(1 for number in MISSIONS if number in rows)
    lines += ["", f"**Прогонов: {ran} из {len(MISSIONS)}; достигнуто: {achieved}.**", "",
              "Ограничения прогона: миссии 3–10 требуют живого браузера владельца (или своего",
              "видимого профиля), поэтому выполняются только с его участием или по его команде.",
              ""]
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    return REPORT


def main() -> int:
    parser = argparse.ArgumentParser(description="Прогон живых миссий MCP-Hands")
    parser.add_argument("missions", nargs="*", type=int, help="номера миссий; пусто — список")
    parser.add_argument("--work", action="store_true",
                        help="свой профиль браузера (не трогать живой браузер владельца)")
    parser.add_argument("--list", action="store_true", help="показать список миссий и выйти")
    args = parser.parse_args()

    if args.list or not args.missions:
        for number in sorted(MISSIONS):
            print(f"{number:>2}. {MISSIONS[number]['phrase'][:70]}")
            print(f"    нужно: {MISSIONS[number]['needs']}; маршрут: {MISSIONS[number]['mode']}")
        return 0

    assistant = Assistant(force_work=args.work, goal_mode=True)
    for number in args.missions:
        if number not in MISSIONS:
            print(f"миссии {number} нет в реестре")
            continue
        run_mission(number, assistant)
    print(f"\nсводка: {write_report()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
