#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Обмен с аддоном Garry's Mod через файлы (игра запрещает Lua ходить на localhost по HTTP).

Папка обмена:  <GarrysMod>/garrysmod/data/j
  аддон пишет   in_<id>.json   {"id": "...", "kind": "chat" | "event", "payload": {...}}
  мост отвечает out_<id>.json  {"id": "...", "reply": "...", "actions": [...]}

Текстовый режим (Left 4 Dead 2): VScript — <left4dead2>/ems/j, плагин SourceMod — <left4dead2>/addons/sourcemod/data/j
  игра пишет    in_<id>.txt    строки  kind=chat|event · user= · id= · game= · type= · text= · end=1 (последняя)
  мост отвечает out_<id>.txt   строки  reply=<строка ответа> (их может быть несколько) · action=<тип>
  Запрос без последней строки end=1 считается недописанным и ждёт (старый битый удаляется через 10 секунд).

Где искать папку игры: переменная GMOD_DATA (в j.env), иначе стандартные места Steam
(в том числе дополнительные библиотеки из libraryfolders.vdf).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

STALE_SECONDS = 10      # битый/недописанный запрос старше этого срока удаляется
OLD_OUT_SECONDS = 120   # ответы, которые аддон так и не забрал


def _steam_roots() -> list[Path]:
    roots: list[Path] = []
    if sys.platform.startswith("win"):
        for env in ("ProgramFiles(x86)", "ProgramFiles"):
            if os.environ.get(env):
                roots.append(Path(os.environ[env]) / "Steam")
        for letter in "CDEFG":
            roots.append(Path(f"{letter}:/Steam"))
            roots.append(Path(f"{letter}:/SteamLibrary"))
    else:
        home = Path.home()
        roots += [home / ".steam" / "steam", home / ".local" / "share" / "Steam",
                  home / "Library" / "Application Support" / "Steam"]
    extra: list[Path] = []
    for root in roots:  # дополнительные библиотеки Steam на других дисках
        vdf = root / "steamapps" / "libraryfolders.vdf"
        try:
            text = vdf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(r'"path"\s+"([^"]+)"', text):
            extra.append(Path(m.group(1).replace("\\\\", "\\")))
    return roots + extra


def find_gmod_data() -> Path | None:
    """Папка garrysmod/data игры или None."""
    custom = os.environ.get("GMOD_DATA", "").strip().strip('"')
    if custom:
        p = Path(custom)
        if p.name.lower() == "j":
            p = p.parent
        return p if p.is_dir() else None
    for root in _steam_roots():
        cand = root / "steamapps" / "common" / "GarrysMod" / "garrysmod" / "data"
        if cand.is_dir():
            return cand
    return None


class FileLink:
    def __init__(self, bridge, data_dir: Path, text_mode: bool = False, game: str = "gmod") -> None:
        self.bridge = bridge
        self.text_mode = text_mode
        self.game = game
        self.ext = "txt" if text_mode else "json"
        self.dir = Path(data_dir) / "j"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.handled = 0

    @staticmethod
    def _parse_text(raw: str) -> dict:
        req = {}
        for line in raw.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                req[key.strip()] = value.strip()
        return req

    def _reply(self, rid: str, out: dict) -> None:
        tmp = self.dir / f"out_{rid}.tmp"
        final = self.dir / f"out_{rid}.{self.ext}"
        if self.text_mode:
            lines = [f"reply={ln}" for ln in str(out.get("reply", "")).splitlines() if ln.strip()]
            lines += [f"action={a['type']}" for a in out.get("actions", [])]
            if "busy" in out:
                lines.append(f"busy={1 if out['busy'] else 0}")
            tmp.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")
        else:
            tmp.write_text(json.dumps({"id": rid, **out}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, final)  # атомарно: аддон никогда не увидит недописанный ответ

    @staticmethod
    def _coerce_val(raw: str):
        s = raw.strip()
        try:
            return float(s) if ("." in s or "e" in s.lower()) else int(s)
        except ValueError:
            return raw

    def poll_once(self) -> int:
        n = 0
        now = time.time()
        for path in sorted(self.dir.glob(f"in_*.{self.ext}")):
            try:
                raw = path.read_text(encoding="utf-8-sig")
                req = self._parse_text(raw) if self.text_mode else json.loads(raw)
                if self.text_mode and ("kind" not in req or req.get("end") != "1"):
                    raise ValueError("запрос ещё дописывается")
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                try:  # возможно, файл ещё дописывается; совсем старый битый — удаляем
                    if now - path.stat().st_mtime > STALE_SECONDS:
                        path.unlink()
                except OSError:
                    pass
                continue
            rid = path.stem[3:] if self.text_mode else str(req.get("id", path.stem[3:]))
            if self.text_mode:  # плоские строки key=value превращаем в тот же payload, что у GMod
                kind = req.get("kind")
                base_keys = {"kind", "end", "user", "id", "game"}
                if kind == "telemetry":
                    tdata = {k: self._coerce_val(v) for k, v in req.items() if k not in base_keys}
                    payload = {"user": req.get("user", ""), "id": req.get("id", ""),
                               "game": req.get("game", self.game or "aerospace"), "data": tdata}
                elif kind == "peer":
                    payload = {
                        "from": req.get("from", ""), "agent": req.get("agent", ""),
                        "user": req.get("user", "N"), "id": req.get("id", ""),
                        "text": req.get("text", ""), "game": req.get("game", "agent"),
                        "peer_task": req.get("peer_task", ""), "n_task": req.get("n_task", ""),
                        "task": req.get("task", ""), "j_task": req.get("j_task", ""),
                        "channel": req.get("channel", f"file:{self.game}"),
                    }
                else:
                    payload = {"user": req.get("user", "player"), "id": req.get("id", ""), "text": req.get("text", ""),
                               "game": req.get("game", self.game), "type": req.get("type", ""), "data": {}}
            else:
                kind, payload = req.get("kind"), req.get("payload") or {}
            try:
                if kind == "chat":
                    out = self.bridge.chat(str(payload.get("user", "player")), str(payload.get("text", "")),
                                           str(payload.get("game", self.game)), str(payload.get("id", "") or ""))
                elif kind == "event":
                    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                    out = {"reply": self.bridge.event(str(payload.get("type", "")), str(payload.get("user", "")),
                                                      data, str(payload.get("game", self.game)))}
                elif kind == "telemetry":
                    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
                    out = self.bridge.telemetry(
                        str(payload.get("game", self.game or "aerospace")),
                        str(payload.get("user", "")),
                        data,
                    )
                elif kind == "peer":
                    j_task = str(payload.get("j_task") if "j_task" in payload else payload.get("task", ""))
                    j_busy = payload.get("j_busy") if "j_busy" in payload else (payload.get("busy") if "busy" in payload else None)
                    out = self.bridge.peer_chat(
                        agent=str(payload.get("from") or payload.get("agent") or payload.get("user") or "N"),
                        text=str(payload.get("text", "")),
                        ident=str(payload.get("id", "")),
                        busy=j_busy,
                        task=j_task,
                        peer_task=str(payload.get("peer_task") or payload.get("n_task") or ""),
                        channel=str(payload.get("channel", f"file:{self.game}")),
                        check_cooldown=bool(payload.get("check_cooldown", False)),
                    )
                else:
                    out = {"reply": ""}
            except Exception as exc:  # один плохой запрос не должен ронять мост
                print("GMod: ошибка обработки запроса:", exc)
                out = {"reply": ""}
            try:
                self._reply(rid, out)
                path.unlink()
                n += 1
            except OSError as exc:
                print("GMod: не удалось записать ответ:", exc)
        for old in list(self.dir.glob("out_*")) + list(self.dir.glob("in_*.tmp")):  # невостребованное и «осиротевшее»
            try:
                if now - old.stat().st_mtime > OLD_OUT_SECONDS:
                    old.unlink()
            except OSError:
                pass
        self.handled += n
        return n

    def run(self, interval: float = 0.2) -> None:
        while True:
            try:
                self.poll_once()
            except Exception as exc:
                print("GMod: сбой цикла обмена:", exc)
            time.sleep(interval)
