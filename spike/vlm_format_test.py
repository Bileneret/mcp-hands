"""Проверка претензии аудитора к замеру VLM: формат координат и калибровка.

Аудитор утверждает, что я сломал grounding сам: модель обучена на канонический формат
`<|box_start|>(x1,y1),(x2,y2)<|box_end|>`, а я требовал JSON в «пикселях скриншота»,
и сверх того искажал координаты повторным масштабированием.

Проверка честная: один и тот же кроп, один и тот же вопрос, но ответ в каноническом формате.
Затем ответ разбирается ТРЕМЯ возможными трактовками — какая из них даёт попадания,
та и есть настоящая система координат модели:

  A. нормировка 0..1000 (как у Qwen2.5-VL) -> пиксели кропа;
  B. абсолютные пиксели внутри картинки, которую видит модель -> пиксели кропа;
  C. абсолютные пиксели исходного кропа без пересчёта.

Плюс отдельно меряется скорость генерации без картинки — проверка объяснения аудитора
«виноваты отсутствующие тензорные ядра GTX 1650».
"""

from __future__ import annotations

import argparse
import ctypes
import json
import re
import sys
import time
from pathlib import Path

SPIKE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SPIKE_DIR))

import dpi_aware  # noqa: E402
import measure_coords as mc  # noqa: E402
import uia_common  # noqa: E402
from vlm_probe import capture_crop, ground_truth, load_model  # noqa: E402

BOX_PROMPT = (
    'Locate the "{name}" element in this window screenshot. '
    "Answer with its bounding box only, in the format "
    "<|box_start|>(x1,y1),(x2,y2)<|box_end|> using the image's own pixel coordinates."
)
BOX_RE = re.compile(r"\((\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\)\s*,\s*"
                    r"\((\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Формат координат VLM: проверка претензии аудита")
    parser.add_argument("--window", default="VLM target")
    parser.add_argument("--targets", default="alpha,beta,gamma")
    parser.add_argument("--max-pixels", type=int, default=401408)
    parser.add_argument("--json", default=str(SPIKE_DIR / "results_vlm_format.json"))
    return parser.parse_args()


def ask_raw(model, processor, image, prompt: str, max_new_tokens: int = 64) -> dict:
    """Как ask() в vlm_probe, но возвращает ещё и размер картинки для модели."""
    import torch

    messages = [{"role": "user", "content": [{"type": "image", "image": image},
                                             {"type": "text", "text": prompt}]}]
    inputs = processor.apply_chat_template(messages, add_generation_prompt=True, tokenize=True,
                                           return_dict=True, return_tensors="pt")
    inputs = {key: (value.to(model.device) if hasattr(value, "to") else value)
              for key, value in inputs.items()}
    grid = None
    if "image_grid_thw" in inputs:
        value = inputs["image_grid_thw"][0].tolist()
        patch = int(getattr(processor.image_processor, "patch_size", 16) or 16)
        grid = {"size": [int(value[2]) * patch, int(value[1]) * patch],
                "llm_tokens": (int(value[1]) // 2) * (int(value[2]) // 2)}
    started = time.perf_counter()
    with torch.inference_mode():
        output = model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    got = output.shape[1] - inputs["input_ids"].shape[1]
    text = processor.batch_decode(output[:, inputs["input_ids"].shape[1]:],
                                  skip_special_tokens=True)[0].strip()
    return {"text": text, "ms": round(elapsed_ms, 1), "tokens": int(got), "image_grid": grid}


def interpretations(box: tuple, grid: dict | None, crop: tuple, origin: tuple) -> dict:
    """Три трактовки ответа модели -> координаты экрана."""
    x1, y1, x2, y2 = box
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    width, height = crop
    out = {}
    out["A: 0..1000"] = (cx / 1000 * width + origin[0], cy / 1000 * height + origin[1])
    if grid and grid.get("size"):
        gw, gh = grid["size"]
        out["B: пиксели картинки модели"] = (cx * width / gw + origin[0], cy * height / gh + origin[1])
    out["C: пиксели кропа"] = (cx + origin[0], cy + origin[1])
    return out


def main() -> int:
    args = parse_args()
    dpi_aware.enable()
    user32, _ = mc.prototypes()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]

    windows = [w for w in uia_common.enum_windows() if args.window in w["title"]]
    if not windows:
        print(f"окно {args.window!r} не найдено")
        return 1
    hwnd = windows[0]["hwnd"]
    rect = uia_common.window_rect(hwnd)
    mc.ensure_foreground(user32, kernel32, hwnd)
    time.sleep(0.4)
    image, crop_ms, source = capture_crop(rect)
    truth = ground_truth(hwnd, [n.strip() for n in args.targets.split(",") if n.strip()])
    print(f"кроп {image.size} за {crop_ms} мс ({source}), эталон: "
          f"{ {k: v['center'] for k, v in truth.items()} }")

    model, processor, info = load_model("Qwen/Qwen3-VL-2B-Instruct", "4bit", "gpu", args.max_pixels)
    print(f"загрузка {info['load_ms']} мс, VRAM {info.get('vram_used_mb')} МБ")

    report = {"crop": {"size": list(image.size), "source": source}, "ground_truth": truth,
              "load": info, "targets": {}}

    # скорость без картинки: проверка объяснения «виновато железо»
    text_only = ask_raw(model, processor, None, "Сколько будет дважды два? Ответь одним словом.", 16)
    print(f"\nбез картинки: {text_only['ms']} мс на {text_only['tokens']} токенов "
          f"= {text_only['tokens'] / max(1e-3, text_only['ms'] / 1000):.1f} ток/с")
    report["text_only"] = text_only

    for name in truth:
        outcome = ask_raw(model, processor, image, BOX_PROMPT.format(name=name))
        match = BOX_RE.search(outcome["text"].replace("<|box_start|>", "(").replace("<|box_end|>", ")"))
        entry = {"ms": outcome["ms"], "raw": outcome["text"][:160],
                 "image_grid": outcome["image_grid"], "box": None, "hits": {}}
        if match:
            box = tuple(float(v) for v in match.groups())
            entry["box"] = list(box)
            for label, point in interpretations(box, outcome["image_grid"], image.size,
                                                (rect["left"], rect["top"])).items():
                target = truth[name]
                hit = (target["left"] <= point[0] <= target["right"]
                       and target["top"] <= point[1] <= target["bottom"])
                distance = ((point[0] - target["center"][0]) ** 2
                            + (point[1] - target["center"][1]) ** 2) ** 0.5
                entry["hits"][label] = {"point": [round(point[0]), round(point[1])],
                                        "hit": hit, "distance_px": round(distance, 1)}
        report["targets"][name] = entry
        parts = []
        for label, data in entry["hits"].items():
            parts.append(f"{label}: " + ("ПОПАЛ" if data["hit"] else f"{data['distance_px']:.0f} px"))
        summary = ", ".join(parts) or "бокс не распознан"
        print(f"  {name:<6} {outcome['ms']:>7.0f} мс | {summary}")
        print(f"         ответ: {outcome['text'][:90]!r}")

    Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nрезультат: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
