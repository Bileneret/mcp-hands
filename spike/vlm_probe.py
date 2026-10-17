"""Локальная VLM: описание кропа окна и grounding с проверкой попадания.

Отвечает на два вопроса из внешнего ревью фактами, а не оценками:
  * влезает ли Qwen3-VL-2B в 4 ГБ VRAM (и в каком квантовании);
  * попадает ли 2B-модель по координатам на мелкие элементы, включая русские подписи.

Grounding проверяется по-настоящему: модели дают кроп окна и просят координаты центра
элемента, а ответ сверяется с прямоугольником этого элемента из UIA. Считается доля
попаданий и расстояние промахов — это и есть метрика качества, а не «на глаз».

    python spike/vlm_probe.py --window "prototype target" --targets alpha,beta,gamma
    python spike/vlm_probe.py --quantization fp16 --targets alpha
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wintypes
import json
import re
import sys
import time
from pathlib import Path

SPIKE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SPIKE_DIR))

import dpi_aware  # noqa: E402
import uia_common  # noqa: E402
from comtypes.gen import UIAutomationClient as UIA  # noqa: E402

GROUNDING_PROMPT = (
    'This is a screenshot of a window. Find the element labeled "{name}". '
    'Answer with JSON only: {{"found": true, "x": <pixel x>, "y": <pixel y>, '
    '"width": <pixel width>, "height": <pixel height>}} where coordinates are in the '
    "screenshot's own pixel grid. If there is no such element, answer {{\"found\": false}}."
)
DESCRIBE_PROMPT = (
    "Это скриншот окна приложения. Перечисли в двух-трёх предложениях, что за окно и какие "
    "элементы управления в нём видны. Ответь по-русски."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Замер локальной VLM на кропе окна")
    parser.add_argument("--window", default="prototype target", help="подстрока заголовка окна")
    parser.add_argument("--model", default="Qwen/Qwen3-VL-2B-Instruct")
    parser.add_argument("--quantization", choices=("4bit", "fp16", "bf16"), default="4bit")
    parser.add_argument("--device-map", choices=("auto", "gpu", "cpu"), default="gpu",
                        help="gpu — грузить сразу на карту (меньше памяти хоста)")
    parser.add_argument("--targets", default="alpha,beta,gamma",
                        help="имена элементов для grounding (через запятую)")
    parser.add_argument("--max-new-tokens", type=int, default=128)
    parser.add_argument("--max-pixels", type=int, default=512 * 28 * 28,
                        help="предел пикселей для процессора: 512*28*28 ~ 512 визуальных токенов")
    parser.add_argument("--activate", action="store_true", default=True,
                        help="поднять окно на передний план перед съёмкой (DXGI снимает экран)")
    parser.add_argument("--save-crop", default=str(SPIKE_DIR / ".vlm_crop.png"))
    parser.add_argument("--json", default=str(SPIKE_DIR / "results_vlm.json"))
    return parser.parse_args()


def ground_truth(hwnd: int, names: list[str]) -> dict:
    """Прямоугольники элементов из UIA — эталон для проверки попаданий."""
    uia = uia_common.create_uia(500)
    result = {}
    for name in names:
        condition = uia.CreatePropertyCondition(UIA.UIA_NamePropertyId, name)
        element = uia.ElementFromHandle(hwnd).FindFirst(UIA.TreeScope_Descendants, condition)
        if element:
            rect = element.CurrentBoundingRectangle
            result[name] = {"left": rect.left, "top": rect.top,
                            "right": rect.right, "bottom": rect.bottom,
                            "center": [(rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2]}
    return result


def capture_crop(rect: dict) -> tuple[object, float, str]:
    """Кроп окна из кадра: DXGI, а при отказе — BitBlt через mss.

    DXGI Desktop Duplication отказывает (DXGI_ERROR_UNSUPPORTED) после того, как дисплей
    уходил в сон, и сам не восстанавливается: слой захвата обязан иметь второй путь,
    иначе агент слепнет ровно тогда, когда его позвали после простоя.
    """
    import numpy as np
    from PIL import Image

    started = time.perf_counter()
    try:
        import dxcam

        camera = dxcam.create(output_color="BGRA")
        try:
            frame = camera.grab()
            if frame is None:
                frame = camera.get_latest_frame()
            crop = frame[rect["top"]:rect["bottom"], rect["left"]:rect["right"]].copy()
        finally:
            camera.release()
        source = "dxcam"
    except Exception as exc:  # noqa: BLE001
        import mss

        with mss.mss() as sct:
            shot = np.array(sct.grab({"left": rect["left"], "top": rect["top"],
                                      "width": rect["right"] - rect["left"],
                                      "height": rect["bottom"] - rect["top"]}))
        crop = shot[:, :, :3][:, :, ::-1]
        source = f"mss (dxcam: {type(exc).__name__})"
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return Image.fromarray(crop), round(elapsed_ms, 2), source


def load_model(name: str, quantization: str, device_map: str = "auto",
               max_pixels: int | None = None) -> tuple[object, object, dict]:
    """Поднимает модель и меряет стоимость загрузки и занятую память."""
    import torch
    from transformers import AutoModelForImageTextToText, AutoProcessor

    info: dict = {"quantization": quantization, "cuda": torch.cuda.is_available()}
    if torch.cuda.is_available():
        info["gpu"] = torch.cuda.get_device_name(0)
        info["vram_total_mb"] = round(torch.cuda.get_device_properties(0).total_memory / 2**20)

    kwargs: dict = {"low_cpu_mem_usage": True}
    if quantization == "4bit":
        from transformers import BitsAndBytesConfig

        kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=torch.float16)
    else:
        dtype = torch.float16 if quantization == "fp16" else torch.bfloat16
        kwargs["dtype"] = dtype
    # На машине с 16 ГБ RAM и занятым файлом подкачки «auto» не поднимается:
    # грузим сразу на карту, чтобы хост держал в памяти только один шард.
    kwargs["device_map"] = {"": 0} if device_map == "gpu" else device_map

    started = time.perf_counter()
    processor = AutoProcessor.from_pretrained(name)
    if max_pixels:
        # В transformers 5.x min_pixels/max_pixels в from_pretrained больше не действуют:
        # размер задаётся полем size у обработчика изображений. Без этого Qwen-обработчик
        # УВЕЛИЧИВАЕТ кроп окна (1125x700 -> 1400x840, ~1500 визуальных токенов),
        # и время ответа определяется префиллом картинки, а не моделью.
        processor.image_processor.size = {"longest_edge": int(max_pixels),
                                          "shortest_edge": 28 * 28 * 4}
    try:
        model = AutoModelForImageTextToText.from_pretrained(name, **kwargs)
    except TypeError:
        # Старые версии transformers знают параметр как torch_dtype.
        if "dtype" in kwargs:
            kwargs["torch_dtype"] = kwargs.pop("dtype")
        model = AutoModelForImageTextToText.from_pretrained(name, **kwargs)
    info["load_ms"] = round((time.perf_counter() - started) * 1000.0)
    if torch.cuda.is_available():
        info["vram_used_mb"] = round(torch.cuda.memory_allocated() / 2**20)
        info["vram_reserved_mb"] = round(torch.cuda.memory_reserved() / 2**20)
    info["device_map"] = str(getattr(model, "hf_device_map", "—"))[:160]
    return model, processor, info


def ask(model, processor, image, prompt: str, max_new_tokens: int) -> dict:
    """Один запрос к модели с замером времени."""
    import torch

    messages = [{"role": "user", "content": [{"type": "image", "image": image},
                                             {"type": "text", "text": prompt}]}]
    inputs = processor.apply_chat_template(messages, add_generation_prompt=True, tokenize=True,
                                           return_dict=True, return_tensors="pt")
    inputs = {key: (value.to(model.device) if hasattr(value, "to") else value)
              for key, value in inputs.items()}
    started = time.perf_counter()
    with torch.inference_mode():
        output = model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    new_tokens = output.shape[1] - inputs["input_ids"].shape[1]
    text = processor.batch_decode(output[:, inputs["input_ids"].shape[1]:],
                                  skip_special_tokens=True)[0]
    grid = None
    if "image_grid_thw" in inputs:
        value = inputs["image_grid_thw"][0].tolist()
        patch = int(getattr(processor.image_processor, "patch_size", 16) or 16)
        # Сетка в патчах: размер картинки, которую реально видит модель. Шаг патча берём
        # у обработчика, а не из головы: у Qwen3-VL это 16, и подстановка 28 (шаг Qwen2-VL)
        # сдвигает пересчёт координат в 1.75 раза — на этом уже была получена ложная цифра.
        grid = {"patches": [int(v) for v in value],
                "patch_size": patch,
                "size": [int(value[2]) * patch, int(value[1]) * patch],
                "llm_tokens": (int(value[1]) // 2) * (int(value[2]) // 2)}
    return {"text": text.strip(), "ms": round(elapsed_ms, 1),
            "prompt_tokens": int(inputs["input_ids"].shape[1]),
            "tokens": int(new_tokens),
            "image_grid": grid,
            "tokens_per_second": round(new_tokens / max(1e-3, elapsed_ms / 1000), 1)}


def scalar(value) -> float:
    """Модель иногда отдаёт координату списком (например [111, 111]) — берём первое число."""
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, (int, float)):
                return float(item)
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def screen_point(parsed: dict, grid: dict | None, crop_size: tuple, window_rect: dict) -> tuple[float, float]:
    """Точка ответа модели -> координаты экрана.

    Модель отвечает в своей сетке: процессор ужимает кроп под лимит визуальных токенов,
    поэтому координаты сначала масштабируются к пикселям кропа, и только потом
    сдвигаются на левый верхний угол окна. Без этих двух шагов сверка с UIA бессмысленна:
    так была получена первая (неверная) оценка 260-292 px.
    """
    x = scalar(parsed.get("x")) + scalar(parsed.get("width")) / 2
    y = scalar(parsed.get("y")) + scalar(parsed.get("height")) / 2
    if grid and grid.get("size"):
        width, height = grid["size"]
        x *= crop_size[0] / max(1, width)
        y *= crop_size[1] / max(1, height)
    return x + window_rect["left"], y + window_rect["top"]


def parse_coordinates(text: str) -> dict | None:
    match = re.search(r"\{.*?\}", text, re.S)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return payload


def main() -> int:
    args = parse_args()
    dpi_aware.enable()

    windows = [window for window in uia_common.enum_windows() if args.window in window["title"]]
    if not windows:
        print(f"окно с заголовком {args.window!r} не найдено")
        return 1
    window = windows[0]
    hwnd = window["hwnd"]
    rect = uia_common.window_rect(hwnd)
    names = [name.strip() for name in args.targets.split(",") if name.strip()]

    report: dict = {"window": {**window, "hwnd": hex(hwnd), "rect": rect},
                    "model": args.model, "targets": names}
    truth = ground_truth(hwnd, names)
    report["ground_truth"] = truth

    if args.activate:
        import measure_coords as mc

        user32, _ = mc.prototypes()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(ctypes.c_ulong)]
        activated = mc.ensure_foreground(user32, kernel32, hwnd)
        time.sleep(0.4)
        report["activated"] = activated
        print(f"Окно поднято на передний план: {activated}")

    image, crop_ms, crop_source = capture_crop(rect)
    image.save(args.save_crop)
    report["crop"] = {"ms": crop_ms, "size": list(image.size), "source": crop_source}
    print(f"Кроп окна: {image.size} за {crop_ms} мс, источник {crop_source} -> {args.save_crop}")

    try:
        model, processor, load_info = load_model(args.model, args.quantization, args.device_map,
                                                 args.max_pixels)
    except Exception as exc:  # noqa: BLE001 — установка модели может не пройти целиком
        report["load_error"] = f"{type(exc).__name__}: {exc}"
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"Модель не поднялась: {type(exc).__name__}: {exc}")
        return 2
    report["load"] = load_info
    print(f"Загрузка: {load_info['load_ms']} мс, VRAM {load_info.get('vram_used_mb', '—')} МБ "
          f"из {load_info.get('vram_total_mb', '—')} МБ, карта: {load_info.get('device_map')}")

    description = ask(model, processor, image, DESCRIBE_PROMPT, args.max_new_tokens)
    report["describe"] = description
    print(f"\nОписание окна ({description['ms']} мс, {description['tokens_per_second']} ток/с):"
          f"\n  {description['text'][:300]}")

    report["grounding"] = []
    hits = 0
    for name in names:
        outcome = ask(model, processor, image, GROUNDING_PROMPT.format(name=name), args.max_new_tokens)
        parsed = parse_coordinates(outcome["text"])
        entry = {"name": name, "ms": outcome["ms"], "raw": outcome["text"][:200], "parsed": parsed,
                 "image_grid": outcome.get("image_grid"), "hit": None, "distance_px": None}
        if parsed and parsed.get("found") and "x" in parsed and "y" in parsed:
            point = screen_point(parsed, outcome.get("image_grid"), image.size, rect)
            entry["point_on_screen"] = [round(value, 1) for value in point]
            if name in truth:
                box = truth[name]
                entry["hit"] = (box["left"] <= point[0] <= box["right"]
                                and box["top"] <= point[1] <= box["bottom"])
                center = box["center"]
                entry["distance_px"] = round(((point[0] - center[0]) ** 2
                                              + (point[1] - center[1]) ** 2) ** 0.5, 1)
            hits += 1 if entry["hit"] else 0
        report["grounding"].append(entry)
        status = "попадание" if entry["hit"] else ("промах" if entry["hit"] is False else "нет ответа")
        distance = entry["distance_px"]
        detail = f", отклонение {distance} px" if distance is not None else ""
        if entry.get("image_grid"):
            detail += f", сетка модели {entry['image_grid']['size']}"
        print(f"  {name:<8} {outcome['ms']:>7} мс -> {status}{detail}"
              f" | ответ: {outcome['text'][:70].replace(chr(10), ' ')}")

    report["hits"] = hits
    report["hit_rate"] = round(hits / max(1, len(names)), 3)
    Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nПопаданий: {hits} из {len(names)} | результат: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
