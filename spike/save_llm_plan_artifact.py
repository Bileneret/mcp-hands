"""Артефакт для v15: живой план b03 от River/GLM-5.3-Flash → strict-схема.

Запуск: python -u spike/save_llm_plan_artifact.py  (нужен config.local.json)
Ключ никогда не попадает в артефакт.
"""
import json
import sys

sys.path.insert(0, r"D:\BOT\MCP-Hands")

from mcp_hands.brain.llm import load_config, request_plan

OUT = r"D:\BOT\MCP-Hands\ForAgentsSpeak\to_review\v15\llm_plan_b03_live.json"


def main() -> None:
    config = load_config()
    result = request_plan(config, "b03", "Word → PDF: сохранение по шаблону имени",
                          ["word_pdf_run"])
    data = {
        "model": config.model,
        "live": True,
        "attempts": result.attempts,
        "plan": result.plan.model_dump() if result.plan else None,
        "error": result.error,
    }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    print("ok:", bool(result.plan), "->", OUT)


if __name__ == "__main__":
    main()
