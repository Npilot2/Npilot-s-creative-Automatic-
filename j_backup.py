#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Резервные копии памяти J.

Что копируется: память (j_memory.json), самомодель (j_self.json), знания (j_knowledge.json), общие заметки
(j_notes.json), правила (j_policy.json), список источников (j_sources.json), характер (j_persona.json),
память мест (j_places.json).
Что НЕ копируется: j.env (там токен — секреты в копии не нужны).

Копии лежат в <данные>/backups/ГГГГ-ММ-ДД_ЧЧММСС/ и ротируются (хранится J_BACKUP_KEEP последних).
Файлы J пишутся атомарно (временный файл + переименование), поэтому копия никогда не получит недописанный файл;
битый JSON в копию не попадает (он пропускается и об этом сообщается).
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from j_paths import data_dir, data_path

# (имя файла по умолчанию, переменная среды с отдельным путём)
DATA_FILES = [
    ("j_memory.json", "J_MEMORY"), ("j_self.json", "J_SELF"), ("j_knowledge.json", "J_KNOWLEDGE"),
    ("j_notes.json", "J_NOTES"), ("j_policy.json", "J_POLICY"), ("j_sources.json", "J_SOURCES"),
    ("j_persona.json", "J_PERSONA"), ("j_places.json", "J_PLACES"),
]


def backups_root() -> Path:
    return data_dir() / "backups"


def data_files() -> list[Path]:
    return [data_path(name, env) for name, env in DATA_FILES]


def make_backup(root: Path | None = None, files: list[Path] | None = None, keep: int = 14) -> tuple[Path | None, list[str]]:
    """Делает копию. Возвращает (папка копии или None, замечания)."""
    root = Path(root) if root else backups_root()
    notes: list[str] = []
    files = files if files is not None else data_files()
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    dest = root / stamp
    copied: list[str] = []
    for src in files:
        if not src.is_file():
            continue
        try:
            json.loads(src.read_text(encoding="utf-8"))  # битый файл в копии бесполезен
        except (OSError, ValueError):
            notes.append(f"{src.name}: файл повреждён или не читается — пропущен")
            continue
        dest.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dest / src.name)
            copied.append(src.name)
        except OSError as exc:
            notes.append(f"{src.name}: не удалось скопировать ({exc})")
    if not copied:
        return None, notes + ["нечего копировать"]
    (dest / "manifest.json").write_text(json.dumps({"created": stamp, "files": copied}, ensure_ascii=False), encoding="utf-8")
    rotate(root, keep)
    return dest, notes


def list_backups(root: Path | None = None) -> list[Path]:
    root = Path(root) if root else backups_root()
    if not root.is_dir():
        return []
    return sorted((p for p in root.iterdir() if p.is_dir() and (p / "manifest.json").exists()), key=lambda p: p.name)


def rotate(root: Path, keep: int) -> list[str]:
    removed: list[str] = []
    items = list_backups(root)
    for old in items[: max(0, len(items) - max(1, keep))]:
        shutil.rmtree(old, ignore_errors=True)
        removed.append(old.name)
    return removed


def latest_age_hours(root: Path | None = None) -> float | None:
    items = list_backups(root)
    if not items:
        return None
    return (time.time() - (items[-1] / "manifest.json").stat().st_mtime) / 3600


def restore(name: str, root: Path | None = None, files: list[Path] | None = None) -> list[str]:
    """Возвращает файлы из копии на место. Ядро при этом должно быть остановлено (иначе оно перезапишет их снова)."""
    root = Path(root) if root else backups_root()
    src_dir = root / name
    if not (src_dir / "manifest.json").exists():
        raise FileNotFoundError(f"копия {name!r} не найдена в {root}")
    targets = {p.name: p for p in (files if files is not None else data_files())}
    restored: list[str] = []
    for item in src_dir.iterdir():
        if item.name == "manifest.json" or item.name not in targets:
            continue
        target = targets[item.name]
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():  # на всякий случай оставляем то, что было, рядом
            shutil.copy2(target, target.with_name(target.name + ".before-restore"))
        tmp = target.with_name(target.name + ".tmp")
        shutil.copy2(item, tmp)
        os.replace(tmp, target)
        restored.append(item.name)
    return restored
