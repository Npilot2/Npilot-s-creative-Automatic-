#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Мост между клиентами (Discord, Minecraft, Garry's Mod, Left 4 Dead 2, голос…) и единым мозгом J.

Клиент говорит с ядром по HTTP на 127.0.0.1 (или через файлы — их обслуживает j_filelink.py):
  POST /chat   {"user": "Ник", "text": "...", "game": "minecraft", "id": "стабильный ID (необязательно)"}
         ->    {"reply": "...", "actions": [{"type": "chop", "count": 8}, ...]}
  POST /event  {"type": "chop_done", "user": "", "data": {"have": 8}, "game": "gmod"}
         ->    {"reply": "..."}
  GET  /health -> {"ok": true, "version": "...", "uptime": секунды}
  GET  /status -> подробности: игры, очередь изучения, число профилей, самомодель

Профиль игрока определяется игрой: см. j_games.py (mc:<ник>, gm:<SteamID64>, ro:<имя>…; у Discord — чистый ID).
Всё, что специфично для игры, лежит в реестре игр, а не здесь.
"""
from __future__ import annotations

import json
import math
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler
from j_ai import PeerGuard, R_BUSY_CLEAR, R_BUSY_SET, is_agent_n, is_local_or_lan_host, is_peer_agent, norm

from j_discord import JDiscordCore
from j_games import ACTION_CANT, ACTION_DOING, get_game
from j_nlu import ActionNLU

VERSION = "2.0.0"
MAX_BODY = 12 * 1024 * 1024


class SafeDict(dict):
    def __missing__(self, key):
        return "?"


class Bridge:
    def __init__(self, core: JDiscordCore | None = None) -> None:
        self.core = core or JDiscordCore()
        self.nlu = ActionNLU(self.core.brain)
        self.lock = threading.RLock()  # запросы приходят из разных потоков (HTTP и файлы)
        self.started = time.time()
        self.telemetry_state: dict[str, dict] = {}
        self.fdir_log: list[dict] = []
        self._in_fault: dict[str, bool] = {}
        self._comms_lost: dict[str, bool] = {}
        self.peer_guard = PeerGuard()
        self.peer_log: list[dict] = []

    def _is_online(self) -> bool:
        fetcher = getattr(getattr(self.core, "knowledge", None), "fetcher", None)
        fn = getattr(fetcher, "is_online", None)
        if callable(fn):
            try:
                return bool(fn())
            except Exception:
                return False
        return False

    @staticmethod
    def _to_float(val) -> float | None:
        if val is None or isinstance(val, bool):
            return None
        try:
            num = float(val)
            return num if math.isfinite(num) else None
        except (TypeError, ValueError):
            return None

    # ---------- приказы: оставляем только то, что умеет игра ----------
    def _for_game(self, res: dict, game_key: str) -> dict:
        game = get_game(game_key)
        allowed = game.supported
        if allowed is None or not res["actions"]:
            return res
        if not allowed:  # игра без приказов
            return {"reply": f"В {game.title} я пока только разговариваю и комментирую игру. Приказы — позже.",
                    "actions": []}
        keep = [a for a in res["actions"] if a["type"] in allowed]
        dropped = [a for a in res["actions"] if a["type"] not in allowed]
        if not dropped:
            return res
        parts = []
        if keep:
            parts.append("Приступаю: " + ", ".join(ACTION_DOING.get(a["type"], a["type"]) for a in keep) + ".")
        names = sorted({ACTION_CANT.get(a["type"], a["type"]) for a in dropped})
        parts.append(f"В {game.title} я этого не умею: {', '.join(names)}.")
        return {"reply": " ".join(parts), "actions": keep}

    # ---------- разговор ----------
    def chat(self, user: str, text: str, game: str = "minecraft", ident: str = "",
             media: list[dict] | None = None) -> dict:
        with self.lock:
            return self._chat(user, text, game, ident, media=media)

    def _chat(self, user: str, text: str, game_key: str, ident: str,
              media: list[dict] | None = None) -> dict:
        game = get_game(game_key)
        uid = game.uid(ident or user)
        brain = self.core.brain
        if hasattr(brain, "resolve_uid"):
            uid = brain.resolve_uid(uid)
        p = brain.profile(uid)
        if not p["name"]:  # ник/имя из клиента берём как имя — без знакомства
            p["name"] = user[:24]
        if not p["lang"]:
            p["lang"] = "ru"
        if ((game_key == "agent") or is_peer_agent(uid, user)) and not media:
            out = self.peer_chat(
                agent=user or "N",
                text=text,
                ident=ident or uid,
                channel=f"chat:{game_key}",
                check_cooldown=False,
            )
            return {"reply": out.get("reply", ""), "actions": []}
        self.peer_guard.note_human(f"chat:{game_key}")
        if hasattr(brain, "peer_guard"):
            brain.peer_guard.note_human(uid)
        try:
            # правила проверяются первыми: запретный запрос не должен «просочиться» через разбор приказов
            gate_ok = self.core.policy.assess(text)["decision"] == "allow"
            # если идёт мини-игра (камень-ножницы-бумага, викторина…) или задан статус занятости, это не приказ
            if (gate_ok and game.orders and not brain.session(uid).state
                    and not (R_BUSY_SET.match(text) or R_BUSY_CLEAR.match(text))):
                res = self.nlu.understand(uid, user, text)
                if res is not None:
                    self.core.me.observe("understood")  # приказ разобран — это тоже удачное понимание
                    kept = self._for_game(res, game_key)
                    acts = kept.get("actions") or []
                    if any(a.get("type") == "stop" for a in acts) and hasattr(brain, "clear_busy"):
                        brain.clear_busy()
                    elif hasattr(brain, "set_busy"):
                        from j_games import ACTION_DOING
                        busy_parts = [
                            ACTION_DOING.get(a.get("type", ""), a.get("type", ""))
                            for a in acts
                            if a.get("type") not in ("status", "list_places", "stop")
                        ]
                        if busy_parts:
                            brain.set_busy(", ".join(busy_parts), duration_sec=180.0, source=game_key)
                    brain.save()
                    return kept
        except Exception as exc:  # понимание приказов не должно ломать обычный разговор
            print("NLU error:", exc)
        return {"reply": self.core.handle(uid, user, text or ("" if media else "привет"), media=media), "actions": []}

    # ---------- события игры ----------
    _DONE_EVENTS = {"finale_win", "place_saved", "place_reached", "charged",
                    "docked", "landed", "orbit_stable", "maneuver_done", "scan_done", "stabilized"}
    _RECOVER_EVENTS = {"extinguished", "comms_restored", "safe_mode_exit"}
    _FAIL_EVENTS = {"stuck", "danger_abort", "hardware_error", "overheat", "battery_low", "no_water",
                    "mine_no_tool", "pickup_fail", "craft_no_materials", "craft_no_table", "craft_unknown",
                    "weapon_none", "attack_miss", "miss",
                    "fuel_low", "radiation_high", "attitude_lost", "comms_lost"}

    def event(self, etype: str, user: str, data: dict, game: str = "minecraft") -> str:
        with self.lock:
            return self._event(etype, user, data, game)

    def peer_chat(
        self,
        agent: str = "N",
        text: str = "",
        ident: str = "",
        busy: bool | None = None,
        task: str = "",
        peer_task: str = "",
        channel: str = "peer",
        check_cooldown: bool = False,
    ) -> dict:
        """Входящее обращение от второго ИИ-агента (по умолчанию N) в автономном/сетевом контуре."""
        brain = self.core.brain
        uid = get_game("agent").uid(ident or agent or "n")
        if hasattr(brain, "resolve_uid"):
            uid = brain.resolve_uid(uid)
        allowed, final_turn = self.peer_guard.allow_turn(
            uid, channel_id=channel, check_cooldown=check_cooldown
        )
        cur_busy = brain.is_busy() if hasattr(brain, "is_busy") else False
        cur_task = brain.current_task() if hasattr(brain, "current_task") else ""
        if not allowed:
            return {
                "ok": True,
                "agent": agent or "N",
                "uid": uid,
                "reply": "",
                "busy": cur_busy,
                "task": cur_task,
                "throttled": True,
                "handoff": False,
            }
        if hasattr(brain, "respond_to_peer"):
            reply = brain.respond_to_peer(
                agent_name=agent or "N",
                text=text,
                uid=uid,
                busy=busy,
                task=task,
                peer_task=peer_task,
            )
        else:
            reply = self.core.handle(uid, agent or "N", text or "привет")
        cur_busy = brain.is_busy() if hasattr(brain, "is_busy") else False
        cur_task = brain.current_task() if hasattr(brain, "current_task") else ""
        entry = {
            "ts": round(time.time(), 2),
            "dir": "in",
            "agent": agent or "N",
            "uid": uid,
            "busy": cur_busy,
            "task": cur_task,
            "peer_task": peer_task or "",
            "text": (text or "")[:160],
            "reply": (reply or "")[:240],
        }
        with self.lock:
            self.peer_log.append(entry)
            if len(self.peer_log) > 100:
                self.peer_log = self.peer_log[-100:]
        return {
            "ok": True,
            "agent": agent or "N",
            "uid": uid,
            "reply": reply,
            "busy": cur_busy,
            "task": cur_task,
            "throttled": False,
            "handoff": final_turn,
        }

    def contact_peer(self, text: str, url: str = "", channel: str = "peer") -> dict:
        """Отправляет сообщение от J второму ИИ-агенту (N) на HTTP endpoint J_N_URL."""
        target = (url or os.environ.get("J_N_URL", "") or "").strip()
        if not target:
            return {"ok": False, "sent": False, "reason": "no_peer_url"}
        parsed = urllib.parse.urlparse(target)
        if (parsed.scheme or "").lower() not in ("http", "https") or not parsed.netloc:
            return {"ok": False, "sent": False, "reason": "invalid_url", "url": target}
        host = (parsed.hostname or "").lower()
        is_local = is_local_or_lan_host(host)
        offline = (os.environ.get("J_OFFLINE_MODE", "0") or "0").strip().lower() in ("1", "true", "yes", "on", "да")
        if offline and not is_local:
            return {"ok": False, "sent": False, "reason": "offline_mode", "url": target}
        brain = self.core.brain
        busy = brain.is_busy() if hasattr(brain, "is_busy") else False
        task = brain.current_task() if hasattr(brain, "current_task") else ""
        try:
            timeout = max(0.5, float(os.environ.get("J_NET_TIMEOUT", "5") or 5))
        except ValueError:
            timeout = 5.0
        payload = {
            "from": "J",
            "user": "J",
            "id": "ag:j",
            "agent": "N",
            "game": "agent",
            "text": (text or "").strip(),
            "busy": busy,
            "task": task,
            "j_busy": busy,
            "j_task": task,
            "channel": channel,
        }
        req = urllib.request.Request(
            target,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw.strip() else {}
            reply = data.get("reply", "") if isinstance(data, dict) else str(data)
            with self.lock:
                self.peer_log.append({
                    "ts": round(time.time(), 2),
                    "dir": "out",
                    "agent": "N",
                    "url": target,
                    "busy": busy,
                    "task": task,
                    "text": payload["text"][:160],
                    "reply": str(reply)[:240],
                })
                if len(self.peer_log) > 100:
                    self.peer_log = self.peer_log[-100:]
            return {"ok": True, "sent": True, "url": target, "reply": reply, "data": data}
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
            with self.lock:
                self.peer_log.append({
                    "ts": round(time.time(), 2),
                    "dir": "out",
                    "agent": "N",
                    "url": target,
                    "busy": busy,
                    "task": task,
                    "text": payload["text"][:160],
                    "error": str(exc)[:200],
                })
                if len(self.peer_log) > 100:
                    self.peer_log = self.peer_log[-100:]
            return {"ok": False, "sent": False, "url": target, "error": str(exc)}

    def _event(self, etype: str, user: str, data: dict, game_key: str) -> str:
        game = get_game(game_key)
        brain = self.core.brain
        if etype in self._RECOVER_EVENTS:
            self.core.me.observe("recovered", task=etype, game=game_key)
        elif etype in self._DONE_EVENTS or etype.endswith("_done"):
            self.core.me.observe("task_done", task=etype, game=game_key)
            if hasattr(brain, "clear_busy"):
                brain.clear_busy()
        elif etype in ("busy", "task_start", "mission_start") and data.get("task") and hasattr(brain, "set_busy"):
            brain.set_busy(str(data["task"]), source=game_key)
        elif etype in self._FAIL_EVENTS or etype.endswith(("_fail", "_none")):
            self.core.me.observe("task_fail", task=etype, game=game_key)
        if etype == "status":
            return game.status_text(data)
        lines = game.event_lines(etype)
        if not lines:
            return ""
        ctx = SafeDict(data)
        if user or "user" not in ctx:
            ctx["user"] = user or ""
        return random.choice(lines).format_map(ctx)

    # ---------- автономная телеметрия и FDIR ----------
    def telemetry(self, game: str = "aerospace", user: str = "", data: dict | None = None) -> dict:
        """Принимает телеметрию борта, оценивает правила FDIR и возвращает автономные команды."""
        with self.lock:
            payload = dict(data) if isinstance(data, dict) else {}
            faults: list[str] = []
            actions: list[dict] = []
            events: list[str] = []

            battery = self._to_float(payload.get("battery"))
            if battery is not None:
                if battery <= 5:
                    faults.append("battery_critical")
                    actions.extend([
                        {"type": "safe_mode", "reason": "battery_critical"},
                        {"type": "dock", "reason": "battery_critical"},
                    ])
                    events.append("battery_low")
                elif battery <= 15:
                    faults.append("battery_low")
                    actions.append({"type": "dock", "reason": "battery_low"})
                    events.append("battery_low")

            temp = self._to_float(payload.get("temp"))
            if temp is not None and temp >= 85:
                faults.append("overheat")
                actions.append({"type": "safe_mode", "reason": "overheat"})
                events.append("overheat")

            attitude = str(payload.get("attitude") or "").strip().lower()
            if attitude in ("lost", "tumbling", "unstable"):
                faults.append("attitude_lost")
                actions.append({"type": "stabilize", "reason": "attitude_lost"})
                events.append("attitude_lost")

            obstacle_dist = self._to_float(payload.get("obstacle_dist"))
            if obstacle_dist is not None and obstacle_dist < 0.5:
                faults.append("obstacle")
                actions.append({"type": "stop", "reason": "obstacle_proximity"})
                events.append("danger_abort")

            radiation = self._to_float(payload.get("radiation"))
            if radiation is not None and radiation >= 50:
                faults.append("radiation_high")
                actions.append({"type": "safe_mode", "reason": "radiation_high"})
                events.append("radiation_high")

            comms = str(payload.get("comms") or "").strip().lower()
            comms_down = comms in ("lost", "offline", "blackout")
            if comms_down:
                self._comms_lost[game] = True
                events.append("comms_lost")
            elif comms and self._comms_lost.get(game):
                self._comms_lost[game] = False
                events.append("comms_restored")

            seen_events: set[str] = set()
            unique_events = [ev for ev in events if not (ev in seen_events or seen_events.add(ev))]

            now = time.time()
            status_text = get_game(game).status_text(payload)
            self.telemetry_state[game] = {
                "ts": now,
                "user": user,
                "data": dict(payload),
                "faults": list(faults),
                "actions": list(actions),
                "status": status_text,
            }

            replies = [msg for ev in unique_events if (msg := self._event(ev, user, payload, game))]
            reply = " ".join(replies)

            if faults:
                self._in_fault[game] = True
                self.fdir_log.append({
                    "ts": now,
                    "game": game,
                    "faults": list(faults),
                    "actions": list(actions),
                    "events": list(unique_events),
                })
                del self.fdir_log[:-200]
                if not any(ev in self._FAIL_EVENTS for ev in unique_events):
                    self.core.me.observe("task_fail", task=",".join(faults), game=game)
                if hasattr(self.core.me, "episode"):
                    self.core.me.episode("fdir", f"FDIR срабатывание ({game}): {', '.join(faults)}.")
                if not reply:
                    reply = f"FDIR ({game}): {', '.join(faults)}."
            elif not comms_down and self._in_fault.get(game):
                self._in_fault[game] = False
                if not any(ev in self._RECOVER_EVENTS for ev in unique_events):
                    self.core.me.observe("recovered", task="fdir", game=game)
                if hasattr(self.core.me, "episode"):
                    self.core.me.episode("fdir", f"FDIR восстановление ({game}): штатный режим.")

            return {
                "ok": True,
                "game": game,
                "autonomous": True,
                "online": self._is_online(),
                "status": status_text,
                "actions": actions,
                "reply": reply,
            }

    # ---------- состояние ----------
    def health(self) -> dict:
        return {"ok": True, "version": VERSION, "uptime": int(time.time() - self.started)}


def make_handler(bridge: Bridge, status_fn=None):
    """HTTP-обработчик ядра. status_fn() — необязательные дополнительные сведения для /status."""

    class Handler(BaseHTTPRequestHandler):
        def _send(self, payload: dict, code: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            try:
                if self.path == "/health":
                    self._send(bridge.health())
                elif self.path == "/status":
                    info = bridge.health()
                    if status_fn:
                        info.update(status_fn())
                    self._send(info)
                elif self.path == "/telemetry":
                    with bridge.lock:
                        self._send({
                            "ok": True,
                            "autonomous": True,
                            "online": bridge._is_online(),
                            "telemetry": dict(bridge.telemetry_state),
                            "fdir_log": list(bridge.fdir_log[-20:]),
                        })
                elif self.path == "/peer":
                    with bridge.lock:
                        brain = bridge.core.brain
                        raw_peers = os.environ.get("J_PEER_AGENTS", "N,Serial Designation N,SD-N,н,дрон н")
                        self._send({
                            "ok": True,
                            "busy": brain.is_busy() if hasattr(brain, "is_busy") else False,
                            "task": brain.current_task() if hasattr(brain, "current_task") else "",
                            "busy_state": dict(getattr(brain, "busy_state", {})),
                            "peer_url": os.environ.get("J_N_URL", "").strip(),
                            "peers": [x.strip() for x in raw_peers.split(",") if x.strip()],
                            "peer_log": list(bridge.peer_log[-20:]),
                        })
                else:
                    self.send_error(404)
            except Exception as exc:
                self._send({"error": str(exc)}, 500)

        def do_POST(self):
            try:
                try:
                    size = int(self.headers.get("Content-Length", 0) or 0)
                except ValueError:
                    self._send({"error": "invalid content-length"}, 400)
                    return
                if size < 0:
                    self._send({"error": "invalid content-length"}, 400)
                    return
                if size > MAX_BODY:
                    self._send({"error": "payload too large"}, 413)
                    return
                raw = self.rfile.read(size) if size else b""
                try:
                    data = json.loads(raw.decode("utf-8") or "{}")
                except (UnicodeDecodeError, ValueError):
                    self._send({"error": "invalid json"}, 400)
                    return
                if not isinstance(data, dict):
                    self._send({"error": "expected json object"}, 400)
                    return
                if self.path == "/chat":
                    media = data.get("media") if isinstance(data.get("media"), list) else None
                    out = bridge.chat(str(data.get("user", "player")), str(data.get("text", "")),
                                      str(data.get("game", "minecraft")), str(data.get("id", "") or ""),
                                      media=media)
                elif self.path == "/event":
                    extra = data.get("data") if isinstance(data.get("data"), dict) else {}
                    etype = str(data.get("type") or data.get("event") or "")
                    out = {"reply": bridge.event(etype, str(data.get("user", "")), extra,
                                                 str(data.get("game", "minecraft")))}
                elif self.path == "/telemetry":
                    extra = data.get("data") if isinstance(data.get("data"), dict) else data
                    out = bridge.telemetry(
                        str(data.get("game", "aerospace")),
                        str(data.get("user", "")),
                        extra,
                    )
                elif self.path == "/peer":
                    action = str(data.get("action", "")).strip().lower()
                    if action == "send" or data.get("outbound"):
                        out = bridge.contact_peer(
                            text=str(data.get("text", "")),
                            url=str(data.get("url", "")),
                            channel=str(data.get("channel", "peer")),
                        )
                    elif "set_busy" in data and not str(data.get("text", "")).strip():
                        brain = bridge.core.brain
                        if data["set_busy"]:
                            brain.set_busy(
                                str(data.get("task") or data["set_busy"]),
                                duration_sec=float(data.get("duration_sec", 300.0) or 300.0),
                                source="peer_api",
                            )
                        else:
                            brain.clear_busy()
                        out = {"ok": True, "busy": brain.is_busy(), "task": brain.current_task()}
                    else:
                        j_task = str(data.get("j_task") if "j_task" in data else data.get("task", ""))
                        j_busy = data.get("j_busy") if "j_busy" in data else (data.get("busy") if "busy" in data else None)
                        out = bridge.peer_chat(
                            agent=str(data.get("from") or data.get("agent") or data.get("user") or "N"),
                            text=str(data.get("text", "")),
                            ident=str(data.get("id", "")),
                            busy=j_busy,
                            task=j_task,
                            peer_task=str(data.get("peer_task") or data.get("n_task") or ""),
                            channel=str(data.get("channel", "peer")),
                            check_cooldown=bool(data.get("check_cooldown", False)),
                        )
                else:
                    self.send_error(404)
                    return
                self._send(out)
            except Exception as exc:  # ядро не должно падать из-за одного кривого запроса
                self._send({"error": str(exc)}, 500)

        def log_message(self, *args):
            pass

    return Handler
