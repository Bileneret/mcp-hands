"""Тесты файловой колонны (b08): политика путей, сортировка, корзина.

Политика — часть контракта безопасности: системные области запрещены до
любых действий; удаление существует только «в корзину».
"""

from __future__ import annotations

import pytest

from mcp_hands.files import (FilesPolicyError, category_of, ensure_allowed, sort_by_type,
                             trash)


def test_category_mapping():
    assert category_of("отчёт.pdf") == "документы"
    assert category_of("ФОТО.PNG") == "изображения"
    assert category_of("data.tar.gz") == "архивы"
    assert category_of("song.flac") == "аудио"
    assert category_of("clip.MP4") == "видео"
    assert category_of("странный.xyz") == "прочее"


def test_policy_forbids_system_and_root_paths():
    with pytest.raises(FilesPolicyError, match="абсолютным"):
        ensure_allowed("относительный/путь")
    with pytest.raises(FilesPolicyError, match="корень диска"):
        ensure_allowed("D:\\")
    forbidden = r"C:\Windows\System32\drivers\etc"
    with pytest.raises(FilesPolicyError, match="системная область"):
        ensure_allowed(forbidden)


def test_sort_dry_run_changes_nothing(tmp_path):
    source = tmp_path / "in"
    target = tmp_path / "out"
    source.mkdir()
    (source / "a.pdf").write_text("x", encoding="utf-8")
    (source / "b.png").write_bytes(b"png")
    outcome = sort_by_type(str(source), str(target), dry_run=True)
    assert outcome["ok"] and outcome["dry_run"] and outcome["count"] == 2
    assert len(outcome["plan"]) == 2 and not target.exists(), "сухой прогон ничего не создаёт"
    assert len(list(source.iterdir())) == 2, "файлы остались на месте"


def test_sort_moves_by_category_without_overwrites(tmp_path):
    source = tmp_path / "in"
    target = tmp_path / "out"
    source.mkdir()
    (source / "a.pdf").write_text("первый", encoding="utf-8")
    (source / "b.png").write_bytes(b"png")
    (source / "c.xyz").write_text("прочее", encoding="utf-8")
    # коллизия: такое имя уже есть в цели — не должно быть перезаписи
    (target / "документы").mkdir(parents=True)
    (target / "документы" / "a.pdf").write_text("ЦЕННЫЙ ФАЙЛ", encoding="utf-8")
    outcome = sort_by_type(str(source), str(target))
    assert outcome["ok"], outcome
    assert outcome["leftovers"] == [] and not any(source.iterdir())
    assert (target / "изображения" / "b.png").exists()
    assert (target / "прочее" / "c.xyz").exists()
    assert (target / "документы" / "a.pdf").read_text(encoding="utf-8") == "ЦЕННЫЙ ФАЙЛ"
    assert (target / "документы" / "a (2).pdf").exists(), "коллизия решена суффиксом"


def test_trash_puts_file_into_recycle_bin(tmp_path):
    victim = tmp_path / "лишний.tmp"
    victim.write_text("временный", encoding="utf-8")
    outcome = trash([str(victim)])
    if not outcome["ok"]:
        pytest.skip(f"корзина недоступна в этой среде: {outcome.get('reason')}")
    assert not victim.exists(), "файл обязан исчезнуть из исходного места"
    assert outcome["trashed"], outcome


def test_trash_refuses_system_path_before_touching(tmp_path):
    with pytest.raises(FilesPolicyError):
        trash([r"C:\Windows\System32\drivers\etc\hosts"])


def test_profile_guard_allows_targets_forbids_configs(tmp_path, monkeypatch):
    """Hardening (аудит v18): корень профиля и AppData запрещены; целевые папки
    (Downloads и подобные) и %LOCALAPPDATA%\\Temp разрешены. На диске у профиля
    папки называются по-английски (Documents, а не «Документы»)."""
    profile = tmp_path / "profile"
    for name in ("Downloads", "Documents", "AppData"):
        (profile / name).mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(profile))
    monkeypatch.setenv("APPDATA", str(profile / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(profile / "AppData" / "Local"))

    ensure_allowed(profile / "Downloads" / "задача.txt")          # рабочая папка: ок
    ensure_allowed(profile / "Documents" / "отчёт.docx")          # ок
    ensure_allowed(profile / "AppData" / "Local" / "Temp" / "p")  # временные: ок
    with pytest.raises(FilesPolicyError, match="корень профиля"):
        ensure_allowed(profile)
    with pytest.raises(FilesPolicyError, match="область профиля запрещена"):
        ensure_allowed(profile / "AppData" / "Roaming" / "Программа")  # конфиги
    with pytest.raises(FilesPolicyError, match="область профиля запрещена"):
        ensure_allowed(profile / "зачем-то-файл-в-корне.txt")
    with pytest.raises(FilesPolicyError, match="область профиля запрещена"):
        ensure_allowed(profile / "AppData" / "Local" / "СтранныйСофт")  # не Temp
