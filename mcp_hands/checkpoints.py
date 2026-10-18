"""Чекпойнты (этап 4, шаг 2): копии файлов перед изменяющими шагами runner.

Схема транзакции: перед шагом резервируются файлы, которые шаг может
затронуть; при неуспехе — восстановление из манифеста ТОЛЬКО своих файлов
прогона, с проверкой хэша. Пользовательские файлы резервированию и откату
не подлежат: runner знает артефакты шага из результатов (L1-detail).

Лимиты (защита от раздувания): общий объём каталога чекпойнтов ограничен;
при переполнении вытесняются самые старые прогоны.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

CHECKPOINTS_DIR = Path(__file__).resolve().parents[1] / "logs" / "checkpoints"
MAX_TOTAL_BYTES = 512 * 1024 * 1024  # 512 МБ на все чекпойнты


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class Checkpoint:
    """Резерв копий файлов шага + манифест."""

    directory: Path
    entries: dict = field(default_factory=dict)  # {путь: {sha256, bytes}}
    ok: bool = False
    reason: str = ""


class CheckpointStore:
    """Каталог чекпойнтов с лимитом объёма и вытеснением старых прогонов."""

    def __init__(self, base: Path | str = CHECKPOINTS_DIR,
                 max_total_bytes: int = MAX_TOTAL_BYTES) -> None:
        self.base = Path(base)
        self.max_total_bytes = max_total_bytes

    def create(self, run_id: str, step_index: int, paths: list[str]) -> Checkpoint:
        """Резервирует перечисленные пути (существующие только; несуществующие
        в манифест с пометкой absent — откат их удалит, если они появились)."""
        stamp = time.strftime("%Y%m%d_%H%M%S")
        directory = self.base / f"{stamp}_{run_id}_step{step_index}"
        directory.mkdir(parents=True, exist_ok=True)
        checkpoint = Checkpoint(directory=directory)
        hashes = []
        for raw in paths:
            path = Path(raw)
            entry = {"absent": True, "sha256": "", "bytes": 0}
            if path.is_file():
                saved = directory / path.name
                counter = 2
                while saved.exists():
                    saved = directory / f"{path.stem} ({counter}){path.suffix}"
                    counter += 1
                shutil.copy2(path, saved)
                entry = {"absent": False, "sha256": file_hash(saved), "bytes": path.stat().st_size,
                         "backup": str(saved)}
                hashes.append(path.stat().st_size)
            checkpoint.entries[str(path)] = entry
        manifest = directory / "manifest.json"
        manifest.write_text(json.dumps(checkpoint.entries, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        checkpoint.ok = True
        self._evict(reserve_bytes=sum(hashes))
        return checkpoint

    def rollback(self, checkpoint: Checkpoint) -> dict:
        """Восстанавливает файлы из манифеста. Появившиеся «из ниоткуда»
        (absent при резерве) удаляются — но ТОЛЬКО если они в списке прогона.
        Откат «нечего восстанавливать» считается удачным (ничего не испорчено)."""
        restored, removed, missing = [], [], []
        for path, entry in checkpoint.entries.items():
            target = Path(path)
            if entry.get("absent"):
                if target.exists():
                    target.unlink()
                    removed.append(str(target))
                continue
            backup = Path(entry.get("backup", ""))
            if backup.is_file() and file_hash(backup) == entry.get("sha256"):
                shutil.copy2(backup, target)
                restored.append(str(target))
            else:
                missing.append(str(target))
        data = {"restored": restored, "removed_appeared": removed, "missing_backups": missing}
        checkpoint.ok = not data["missing_backups"]
        data["ok"] = checkpoint.ok
        return data

    def _evict(self, reserve_bytes: int = 0) -> None:
        total = 0
        candidates: list[tuple[float, Path]] = []
        for child in self.base.iterdir() if self.base.exists() else []:
            if not child.is_dir():
                continue
            size = sum(f.stat().st_size for f in child.rglob("*") if f.is_file())
            total += size
            candidates.append((child.stat().st_mtime, child))
        budget = self.max_total_bytes - reserve_bytes
        for _mtime, child in sorted(candidates):
            if total <= budget:
                return
            size = sum(f.stat().st_size for f in child.rglob("*") if f.is_file())
            shutil.rmtree(child, ignore_errors=True)
            total -= size
