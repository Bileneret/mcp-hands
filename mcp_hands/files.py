"""Файловые операции (этап 4-пре, колонна B): разбор папок и удаление ТОЛЬКО в корзину.

Политика (опросник владельца, не подлежит смягчению):
- **Удаление только в корзину** (`SHFileOperation` + `FOF_ALLOWUNDO`). Полного
  удаления в наборе не существует. Если система настроена «не использовать
  корзину», файл может исчезнуть безвозвратно — это детектируется сверкой
  объёма корзины до/после и отмечается в результате как предупреждение.
- **Запрещённые области** (системные): SystemRoot, Program Files, Program Files
  (x86), ProgramData и корни дисков — операция отказывает до любых действий.
- **Никаких перезаписей**: при совпадении имени в цели файл получает суффикс.

Сортировка (`sort_by_type`) — перемещение по категориям (документы, изображения,
архивы, аудио, видео, прочее) в подпапки цели; `dry_run` показывает план без
единого изменения (файлы, которые были бы перемещены, и коллизии).
"""

from __future__ import annotations

import ctypes
import os
import shutil
from ctypes import wintypes
from pathlib import Path

FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400
FOF_NOCONFIRMMKDIR = 0x0200


class FilesPolicyError(ValueError):
    """Отказ политики: путь запрещён к изменению (системная область и пр.)."""


CATEGORIES: dict[str, set[str]] = {
    "документы": {".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt", ".xls", ".xlsx",
                  ".csv", ".ppt", ".pptx", ".md"},
    "изображения": {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff",
                    ".svg"},
    "архивы": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"},
    "аудио": {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac"},
    "видео": {".mp4", ".mkv", ".avi", ".mov", ".webm", ".wmv"},
}
OTHER = "прочее"


def category_of(name: str) -> str:
    suffix = Path(name).suffix.lower()
    for category, extensions in CATEGORIES.items():
        if suffix in extensions:
            return category
    return OTHER


def _forbidden_roots() -> list[Path]:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    program_files_x86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    program_data = Path(os.environ.get("ProgramData", r"C:\ProgramData"))
    return [system_root, program_files, program_files_x86, program_data]


# Разрешённые подпапки профиля пользователя (hardening по рекомендации аудита v18):
# корень профиля, %APPDATA% и %LOCALAPPDATA% запрещены — там конфигурации и
# хранилища программ; целевые пользовательские папки (Downloads и подобные)
# остаются рабочими для задач.
PROFILE_ALLOWED_SUBDIRS = {"downloads", "documents", "desktop",
                           "pictures", "videos", "music"}


def _profile_guard(resolved: Path) -> None:
    """Корень профиля запрещён, кроме целевых пользовательских папок;
    внутри AppData/LocalAppData разрешён только каталог Temp (временные файлы
    тестов и прогонов)."""
    profile = Path(os.environ.get("USERPROFILE", "")).resolve(strict=False)
    if profile == Path(""):
        return
    under_profile = profile in resolved.parents
    if resolved != profile and not under_profile:
        return
    if resolved == profile:
        raise FilesPolicyError(f"корень профиля пользователя менять нельзя: {resolved}")
    first = resolved.relative_to(profile).parts[0].lower()
    if first in PROFILE_ALLOWED_SUBDIRS:
        return
    if first == "appdata":
        # внутри AppData разрешён только Temp: pytest и временные каталоги прогонов
        parts = [part.lower() for part in resolved.relative_to(profile).parts]
        if len(parts) >= 3 and parts[1] == "local" and parts[2] == "temp":
            return
    raise FilesPolicyError(
        f"область профиля запрещена; рабочие папки: Downloads/Документы/"
        f"Рабочий стол/Картинки/Видео/Музыка и %LOCALAPPDATA%\\Temp — "
        f"а указано: {resolved}")


def ensure_allowed(path: Path) -> Path:
    """Путь разрешён к изменению: абсолютный, не корень диска, не под системной областью."""
    target = Path(path)
    if not target.is_absolute():
        raise FilesPolicyError(f"путь должен быть абсолютным: {target}")
    resolved = target.resolve(strict=False)
    if resolved.parent == resolved:
        raise FilesPolicyError(f"корень диска менять нельзя: {resolved}")
    for root in _forbidden_roots():
        try:
            root_resolved = root.resolve(strict=False)
        except OSError:
            continue
        if resolved == root_resolved or root_resolved in resolved.parents:
            raise FilesPolicyError(f"системная область запрещена: {resolved}")
    _profile_guard(resolved)
    return resolved


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND),
                ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR),
                ("fFlags", ctypes.c_ushort),
                ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", wintypes.LPCWSTR)]


class _SHQUERYRBINFO(ctypes.Structure):
    """Структура Windows для SHQueryRecycleBinW (важно выравнивание: 4 + 8 + 8)."""

    _fields_ = [("cbSize", wintypes.DWORD),
                ("i64Size", ctypes.c_ulonglong),
                ("i64NumItems", ctypes.c_longlong)]


def _recycle_usage(path: Path) -> int | None:
    """Занято в корзине на диске пути, байт; None — недоступно."""
    try:
        info = _SHQUERYRBINFO()
        info.cbSize = ctypes.sizeof(_SHQUERYRBINFO)
        drive = Path(path).drive + "\\"
        result = ctypes.windll.shell32.SHQueryRecycleBinW(drive, ctypes.byref(info))
        if result != 0:
            return None
        return int(info.i64Size)
    except Exception:  # noqa: BLE001 — диагностическая метрика, не критичный путь
        return None


def trash(paths: list[str]) -> dict:
    """Перемещает пути в корзину (SHFileOperation, FOF_ALLOWUNDO). Никогда не удаляет полностью сам."""
    if not paths:
        return {"ok": False, "reason": "пустой список путей", "trashed": []}
    checked: list[Path] = []
    for item in paths:
        checked.append(ensure_allowed(item))
    missing = [str(p) for p in checked if not p.exists()]
    if missing:
        return {"ok": False, "reason": f"не найдены: {missing}", "trashed": []}
    before = {p.drive: _recycle_usage(p) for p in checked}
    from_buffer = "\x00".join(str(p) for p in checked) + "\x00\x00"
    operation = _SHFILEOPSTRUCTW()
    operation.wFunc = FO_DELETE
    operation.pFrom = from_buffer
    operation.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0:
        return {"ok": False, "reason": f"SHFileOperation вернул код {result}",
                "trashed": [], "aborted": bool(operation.fAnyOperationsAborted)}
    gone = [str(p) for p in checked if not p.exists()]
    warnings: list[str] = []
    recycle_grew = False
    for path in checked:
        before_value = before.get(path.drive)
        after_value = _recycle_usage(path)
        if before_value is not None and after_value is not None and after_value > before_value:
            recycle_grew = True
    if gone and not recycle_grew:
        warnings.append("корзина не выросла: возможна настройка «удалять мимо корзины» — "
                        "проверьте политику системы")
    return {"ok": len(gone) == len(checked), "trashed": gone,
            "aborted": bool(operation.fAnyOperationsAborted), "warnings": warnings}


def _unique_target(target: Path) -> Path:
    """Никаких перезаписей: занятое имя получает числовой суффикс."""
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    counter = 2
    while True:
        candidate = target.with_name(f"{stem} ({counter}){suffix}")
        if not candidate.exists():
            return candidate
        counter += 1
        if counter > 9999:
            raise FilesPolicyError(f"не удалось подобрать имя для {target}")


def sort_by_type(source: str, target_root: str, dry_run: bool = False) -> dict:
    """Разбирает файлы верхнего уровня source по категориям в подпапки target_root."""
    resolved_source = ensure_allowed(source)
    resolved_target = ensure_allowed(target_root)
    if not resolved_source.is_dir():
        return {"ok": False, "reason": f"нет папки-источника: {resolved_source}"}
    files = [p for p in sorted(resolved_source.iterdir()) if p.is_file()]
    planned: list[dict] = []
    moved: list[dict] = []
    for item in files:
        category = category_of(item.name)
        destination_dir = resolved_target / category
        target = _unique_target(destination_dir / item.name)
        entry = {"file": item.name, "category": category,
                 "target": str(target)}
        planned.append(entry)
        if dry_run:
            entry["collision"] = target.exists()
            continue
        destination_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(item), str(target))
        moved.append(entry)
    result = {"ok": True, "dry_run": dry_run, "source": str(resolved_source),
              "target_root": str(resolved_target), "count": len(planned)}
    if dry_run:
        result["plan"] = planned
    else:
        result["moved"] = moved
    # постусловие: при боевом прогоне ни одного файла не осталось в источнике
    if not dry_run:
        leftovers = [p.name for p in resolved_source.iterdir() if p.is_file()]
        result["leftovers"] = leftovers
        result["ok"] = not leftovers
        if leftovers:
            result["reason"] = f"в источнике остались файлы: {leftovers[:5]}"
    return result
