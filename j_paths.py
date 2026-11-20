#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Где J хранит свои данные.

По умолчанию — рядом с кодом (как раньше). Если задать J_DATA_DIR, ВСЕ данные (память, самомодель, знания, заметки,
правила, источники, резервные копии, журналы) уйдут в эту папку — например, вне OneDrive: C:\\JData.
Отдельный файл можно переопределить своей переменной (J_MEMORY, J_SELF, J_KNOWLEDGE, J_NOTES, J_POLICY, J_SOURCES, J_PERSONA, J_PLACES).
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
_WRITE_LOCK = threading.RLock()


def data_dir() -> Path:
    raw = os.environ.get("J_DATA_DIR", "").strip().strip('"')
    d = Path(raw) if raw else CODE_DIR
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def data_path(name: str, env: str | None = None) -> Path:
    """Путь к файлу данных: свой env-override > J_DATA_DIR > папка с кодом."""
    if env:
        custom = os.environ.get(env, "").strip().strip('"')
        if custom:
            return Path(custom)
    return data_dir() / name


def atomic_write_json(path: Path, data, backup: bool = True, indent: int = 1) -> bool:
    """Атомарно записывает JSON: создаёт папку, сохраняет валидный .bak, пишет в .tmp и заменяет с повторами."""
    path = Path(path)
    payload = json.dumps(data, ensure_ascii=False, indent=indent)
    with _WRITE_LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        if backup and path.is_file():
            try:
                json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                pass
            else:
                try:
                    shutil.copy2(path, path.with_suffix(".bak"))
                except OSError:
                    pass
        tmp = path.with_suffix(".tmp") if path.suffix != ".tmp" else path.with_name(path.name + ".tmp")
        wrote = False
        for attempt in range(3):
            try:
                tmp.write_text(payload, encoding="utf-8")
                wrote = True
                break
            except PermissionError:
                if attempt < 2:
                    time.sleep(0.05)
            except OSError:
                break
        if not wrote:
            return False
        for attempt in range(3):
            try:
                os.replace(tmp, path)
                return True
            except PermissionError:
                if attempt < 2:
                    time.sleep(0.05)
            except OSError:
                break
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
