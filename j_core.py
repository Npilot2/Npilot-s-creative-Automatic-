#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ядро J: ОДИН процесс с единым мозгом, единой памятью и единой самомоделью.

Что в нём работает:
  • мозг (характер, память, правила, знания, самомодель);
  • HTTP-сервер на 127.0.0.1:J_PORT — для Discord, Minecraft, голоса;
  • файловые каналы Garry's Mod и Left 4 Dead 2 (если папки игр найдены);
  • фоновое изучение тем по расписанию (J_STUDY_MINUTES);
  • автоматические резервные копии памяти (J_BACKUP_HOURS).
Клиенты (Discord-бот, голос, Minecraft-бот, аддоны игр) — «тонкие»: они только передают ядру слова и события.

Запуск:  python j_core.py   или   python j_launcher.py start core
"""
from __future__ import annotations

import atexit
import json
import os
import signal
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from j_backup import latest_age_hours, make_backup
from j_bridge import VERSION, Bridge, make_handler
from j_config import effective_values, parse_env_file, validate
from j_discord import load_env
from j_filelink import FileLink, find_gmod_data
from j_games import GAMES
from j_paths import data_dir

HOST = "127.0.0.1"  # по умолчанию только localhost; J_HOST=0.0.0.0 открывает доступ по LAN для роботов и агента N


def host() -> str:
    return (os.environ.get("J_HOST", HOST) or HOST).strip() or HOST


def port() -> int:
    try:
        return int(os.environ.get("J_PORT", "8765") or 8765)
    except ValueError:
        return 8765


def _autonomy_flags() -> tuple[bool, bool]:
    """Возвращает (autonomous, offline_mode) по переменным среды."""
    offline_mode = os.environ.get("J_OFFLINE_MODE", "0").strip().lower() in ("1", "true", "yes", "on", "да")
    autonomous = os.environ.get("J_AUTONOMOUS", "1").strip().lower() not in ("0", "false", "no", "off", "нет")
    return autonomous, offline_mode


def _dir_file_link(bridge: Bridge, env_key: str, **kwargs) -> FileLink | None:
    """Создаёт FileLink по переменной среды с папкой обмена, если папка существует."""
    raw = os.environ.get(env_key, "").strip().strip('"')
    if not raw:
        return None
    p = Path(raw)
    p = p.parent if p.name.lower() == "j" else p
    return FileLink(bridge, p, **kwargs) if p.is_dir() else None


def prepare() -> tuple[Bridge, "FileLink | None", "FileLink | None"]:
    """Загружает j.env, создаёт мозг и каналы обмена с играми (GMod, L4D2) и телеметрией. Серверы и потоки не запускает."""
    load_env()
    bridge = Bridge()
    gmod_dir = find_gmod_data()
    gmod = FileLink(bridge, gmod_dir) if gmod_dir else None
    l4d2 = _dir_file_link(bridge, "L4D2_DATA", text_mode=True, game="l4d2")
    bridge.telemetry_link = _dir_file_link(bridge, "J_TELEMETRY_DIR", game="aerospace")
    return bridge, gmod, l4d2


def is_core_running(url: str | None = None) -> dict | None:
    """Если ядро уже отвечает на /health — возвращает его ответ."""
    if url is None:
        bind_h = host()
        probe_h = "127.0.0.1" if bind_h in ("0.0.0.0", "::", "") else bind_h
        url = f"http://{probe_h}:{port()}"
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=1.5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


class Core:
    def __init__(self) -> None:
        self.bridge, self.gmod, self.l4d2 = prepare()
        self.telemetry_link: FileLink | None = (
            self.bridge.telemetry_link
            if hasattr(self.bridge, "telemetry_link")
            else _dir_file_link(self.bridge, "J_TELEMETRY_DIR", game="aerospace")
        )
        self.brain_core = self.bridge.core
        self.stop_event = threading.Event()
        self.server: ThreadingHTTPServer | None = None
        self.threads: list[threading.Thread] = []
        self.last_backup: Path | None = None

    # ---------- фоновые задачи ----------
    def _study_loop(self) -> None:
        try:
            minutes = float(os.environ.get("J_STUDY_MINUTES", "30") or 0)
        except ValueError:
            minutes = 30.0
        if minutes <= 0:
            return
        while not self.stop_event.wait(minutes * 60):
            try:
                stats = self.brain_core.knowledge.study_next()
                if stats:
                    self.brain_core.me.observe("studied", topic=stats["topic"], added=stats["added"])
                    print(f"[изучение] «{stats['topic']}»: найдено {stats['found']}, на проверку {stats['added']}, "
                          f"отсеяно {stats['skipped']}")
            except Exception as exc:
                print("[изучение] сбой:", exc)

    def backup_now(self) -> Path | None:
        try:
            keep = int(os.environ.get("J_BACKUP_KEEP", "14") or 14)
        except ValueError:
            keep = 14
        dest, notes = make_backup(keep=keep)
        for n in notes:
            print("[копия]", n)
        if dest:
            self.last_backup = dest
            print(f"[копия] сохранена: {dest.name}")
        return dest

    def _backup_loop(self) -> None:
        try:
            hours = float(os.environ.get("J_BACKUP_HOURS", "12") or 0)
        except ValueError:
            hours = 12.0
        if hours <= 0:
            return
        age = latest_age_hours()
        if age is None or age >= hours:
            self.backup_now()
        while not self.stop_event.wait(hours * 3600):
            self.backup_now()

    def _link_loop(self, link: FileLink) -> None:
        while not self.stop_event.is_set():
            try:
                link.poll_once()
            except Exception as exc:
                print("[обмен файлами] сбой:", exc)
            self.stop_event.wait(0.2)

    # ---------- состояние для /status ----------
    def status(self) -> dict:
        with self.bridge.lock:
            c = self.brain_core
            st = c.knowledge.status()
            is_online_fn = getattr(c.knowledge.fetcher, "is_online", None)
            online = bool(is_online_fn()) if callable(is_online_fn) else False
            autonomous, offline_mode = _autonomy_flags()
            return {
                "host": host(),
                "port": port(),
                "games": sorted(GAMES),
                "data_dir": str(data_dir()),
                "links": {
                    "gmod": str(self.gmod.dir) if self.gmod else None,
                    "l4d2": str(self.l4d2.dir) if self.l4d2 else None,
                    "telemetry": str(self.telemetry_link.dir) if self.telemetry_link else None,
                },
                "profiles": len(c.brain.users),
                "knowledge": {"queue": len(st["queue"]), "pending": st["pending"], "approved": st["approved"]},
                "self": {"starts": c.me.data["starts"], "updates": c.me.data["updates"]},
                "owners": len(c.owners), "developers": len(c.developers),
                "last_backup_hours": latest_age_hours(),
                "autonomous": autonomous,
                "offline_mode": offline_mode,
                "online": online,
            }

    # ---------- запуск ----------
    def start(self) -> int:
        running = is_core_running()
        if running:
            print(f"Ядро J уже запущено (версия {running.get('version')}). Второй экземпляр не нужен.")
            return 2
        bind_host = host()
        try:
            self.server = ThreadingHTTPServer((bind_host, port()), make_handler(self.bridge, self.status))
        except OSError as exc:
            print(f"Не удалось занять {bind_host}:{port()} ({exc}). Проверь J_HOST / J_PORT в j.env")
            return 3
        self.server.daemon_threads = True

        self._banner()
        run_dir = data_dir() / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        pid_file = run_dir / "core.pid"
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        atexit.register(lambda: pid_file.unlink(missing_ok=True))
        atexit.register(self._save_all)

        tasks = [self._study_loop, self._backup_loop]
        if self.gmod:
            tasks.append(lambda: self._link_loop(self.gmod))
        if self.l4d2:
            tasks.append(lambda: self._link_loop(self.l4d2))
        if self.telemetry_link:
            tasks.append(lambda: self._link_loop(self.telemetry_link))
        for fn in tasks:
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self.threads.append(t)

        for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
            if sig is not None:
                try:
                    signal.signal(sig, lambda *_: self.stop())
                except (ValueError, OSError):
                    pass
        try:
            self.server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
        return 0

    def stop(self) -> None:
        self.stop_event.set()
        if self.server:
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    def _save_all(self) -> None:
        try:
            self.brain_core.brain.save()
            self.brain_core.me.save()
        except Exception:
            pass

    def _banner(self) -> None:
        c = self.brain_core
        print(f"=== Ядро J v{VERSION} ===")
        print(f"Адрес: http://{host()}:{port()}   Данные: {data_dir()}")
        print(f"Профилей: {len(c.brain.users)} · владельцев: {len(c.owners)} · разработчиков: {len(c.developers)}")
        info = c.me.startup_info
        print("Самомодель: " + ("первый запуск." if info["first"] else
              f"запуск №{c.me.data['starts']}" + (", код обновлён" if info["code_changed"] else "") +
              (", правила изменены" if info["rules_changed"] else "") + "."))
        print(f"GMod: {self.gmod.dir if self.gmod else 'не настроено'}")
        print(f"Left 4 Dead 2: {self.l4d2.dir if self.l4d2 else 'не настроено'}")
        print(f"Телеметрия (файлы): {self.telemetry_link.dir if self.telemetry_link else 'не настроено'}")
        autonomous, offline_mode = _autonomy_flags()
        mode_str = "строгий офлайн" if offline_mode else ("гибридный (автономный приоритет)" if autonomous else "сетевой")
        try:
            import j_web
            kb_count = len(getattr(j_web, "BUILTIN_AEROSPACE_KB", ()) or ())
        except Exception:
            kb_count = 0
        print(f"Автономность: {mode_str} · бортовой архив (аэрокосмос/робототехника): {kb_count} статей")
        if c.policy.error:
            print("ВНИМАНИЕ (правила):", c.policy.error)
        if getattr(c.brain, "persona_error", ""):
            print("ВНИМАНИЕ (характер):", c.brain.persona_error)
        env_path = Path(os.environ.get("J_ENV") or Path(__file__).resolve().parent / "j.env")
        _, file_issues = parse_env_file(env_path)
        for issue in file_issues + validate(effective_values(env_path)):
            if issue.level != "INFO":
                print(f"ВНИМАНИЕ (настройки) {issue.key}: {issue.message}")
        if not c.owners:
            print("ВНИМАНИЕ: J_OWNERS не задан — команды !study/!review/!approve/!reject никому не доступны.")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    return Core().start()


if __name__ == "__main__":
    sys.exit(main())
