#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Единое управление J вместо россыпи .bat-файлов (их блокирует Windows, а .py запускается всегда).

  python j_launcher.py                 — меню
  python j_launcher.py install [--yes] — первичная установка (j.env, файлы данных, зависимости, аддон GMod, doctor)
  python j_launcher.py doctor          — диагностика: что у меня не так
  python j_launcher.py init            — создать j.env.example (и j.env, если его нет)
  python j_launcher.py start [core|offline|discord|telegram|whatsapp|voice|all]   — запустить (offline = только ядро без ботов)
  python j_launcher.py stop  [core|discord|telegram|whatsapp|voice|all]   — остановить
  python j_launcher.py status          — кто работает
  python j_launcher.py logs core [строк]  — последние строки журнала
  python j_launcher.py backup          — сделать копию памяти / показать список копий
  python j_launcher.py restore [имя]   — вернуть память из копии (ядро должно быть остановлено)
  python j_launcher.py env             — действующие настройки (секреты скрыты)
  python j_launcher.py test            — запустить тесты проекта

Ключ --window открывает компонент в отдельном окне консоли (Windows). Голос всегда запускается в окне.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from j_backup import list_backups, make_backup, restore
from j_config import BY_KEY, effective_values, mask, render_example
from j_discord import load_env
from j_paths import CODE_DIR, atomic_write_json, data_dir, data_path

COMPONENTS = {
    "core": ("j_core.py", "Ядро J"),
    "discord": ("j_discord.py", "Discord-бот"),
    "telegram": ("j_telegram.py", "Telegram-бот"),
    "whatsapp": ("j_whatsapp.py", "WhatsApp-бот"),
    "voice": ("j_voice.py", "Голосовой помощник"),
}
ORDER = ["core", "discord", "telegram", "whatsapp"]  # что запускает «all» (боты без токена пропускаются в all)


# ---------- процессы ----------
def run_dir() -> Path:
    d = data_dir() / "run"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_path(name: str) -> Path:
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{name}.log"


def read_pid(name: str) -> int | None:
    try:
        return int((run_dir() / f"{name}.pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def is_running(name: str) -> bool:
    return pid_alive(read_pid(name))


def _python_for(name: str) -> str:
    if name == "voice":
        return effective_values().get("J_VOICE_PYTHON") or sys.executable
    return sys.executable


def start_component(name: str, window: bool = False, extra_env: dict[str, str] | None = None) -> tuple[bool, str]:
    script, title = COMPONENTS[name]
    if is_running(name):
        return True, f"{title}: уже запущен (PID {read_pid(name)})"
    if not (CODE_DIR / script).is_file():
        return False, f"{title}: не найден файл {script}"
    if name == "discord" and not effective_values().get("DISCORD_TOKEN"):
        return False, "Discord-бот: в j.env не задан DISCORD_TOKEN"
    if name == "telegram" and not effective_values().get("TELEGRAM_TOKEN"):
        return False, "Telegram-бот: в j.env не задан TELEGRAM_TOKEN"
    if name == "whatsapp" and (not effective_values().get("WHATSAPP_TOKEN") or not effective_values().get("WHATSAPP_PHONE_ID")):
        return False, "WhatsApp-бот: в j.env не заданы WHATSAPP_TOKEN или WHATSAPP_PHONE_ID"
    env = dict(os.environ, PYTHONIOENCODING="utf-8", **(extra_env or {}))
    cmd = [_python_for(name), "-u", str(CODE_DIR / script)]
    flags = 0
    stdout = None
    if os.name == "nt":
        window = window or name == "voice"
        flags = subprocess.CREATE_NEW_CONSOLE if window else subprocess.CREATE_NO_WINDOW
    if not (os.name == "nt" and window):
        lp = log_path(name)
        if lp.exists() and lp.stat().st_size > 2_000_000:  # простая ротация журнала
            lp.replace(lp.with_suffix(".log.1"))
        stdout = open(lp, "ab")
    try:
        proc = subprocess.Popen(cmd, cwd=CODE_DIR, env=env, stdin=subprocess.DEVNULL if name != "voice" else None,
                                stdout=stdout, stderr=subprocess.STDOUT if stdout else None, creationflags=flags)
    except OSError as exc:
        return False, f"{title}: не удалось запустить ({exc})"
    (run_dir() / f"{name}.pid").write_text(str(proc.pid), encoding="utf-8")
    time.sleep(0.8)
    if proc.poll() is not None:
        return False, f"{title}: завершился сразу (код {proc.returncode}). Смотри: python j_launcher.py logs {name}"
    return True, f"{title}: запущен (PID {proc.pid})"


def wait_core(timeout: float = 25.0) -> bool:
    from j_core import is_core_running
    end = time.time() + timeout
    while time.time() < end:
        if is_core_running():
            return True
        time.sleep(0.5)
    return False


def stop_component(name: str) -> str:
    title = COMPONENTS[name][1]
    pid = read_pid(name)
    if not pid_alive(pid):
        (run_dir() / f"{name}.pid").unlink(missing_ok=True)
        return f"{title}: не запущен"
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        import signal
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            if not pid_alive(pid):
                break
            time.sleep(0.1)
        if pid_alive(pid):
            os.kill(pid, signal.SIGKILL)
    (run_dir() / f"{name}.pid").unlink(missing_ok=True)
    return f"{title}: остановлен"


def status_lines() -> list[str]:
    from j_core import is_core_running
    lines = []
    for name, (_, title) in COMPONENTS.items():
        pid = read_pid(name)
        alive = pid_alive(pid)
        extra = ""
        if name == "core":
            info = is_core_running()
            extra = f", отвечает (v{info.get('version')}, аптайм {info.get('uptime')} с)" if info else (", НЕ отвечает" if alive else "")
        lines.append(f"{'●' if alive else '○'} {title}: {'работает (PID ' + str(pid) + ')' if alive else 'не запущен'}{extra}")
    return lines


def targets(arg: str | None) -> list[str]:
    if not arg or arg == "all":
        return list(ORDER)
    if arg == "offline":
        return ["core"]
    if arg not in COMPONENTS:
        raise SystemExit(f"Неизвестный компонент {arg!r}. Доступно: {', '.join(COMPONENTS)}, offline, all")
    return [arg]


# ---------- команды ----------
def cmd_init() -> int:
    example = CODE_DIR / "j.env.example"
    example.write_text(render_example(), encoding="utf-8")
    print(f"Создан {example.name}: в нём все настройки с пояснениями.")
    env = CODE_DIR / "j.env"
    if env.exists():
        print("j.env уже есть — не трогаю.")
    else:
        env.write_text(render_example(), encoding="utf-8")
        print("Создан j.env (всё закомментировано). Автономное ядро и голос работают сразу без токенов.")
        print("Для ботов или настройки автономности открой j.env и раскомментируй нужные строки.")
    return 0


def cmd_install(yes: bool = False) -> int:
    """Первичная установка и обновление компонентов J."""
    cmd_init()
    load_env()

    d = data_dir()
    from j_policy import load_rules
    from j_web import load_sources
    load_rules()
    load_sources()

    persona_dst = data_path("j_persona.json", "J_PERSONA")
    code_persona = CODE_DIR / "j_persona.json"
    try:
        import j_ai
        load_persona = getattr(j_ai, "load_persona", None)
        if callable(load_persona):
            persona_data, _ = load_persona()
            if not persona_dst.exists():
                if code_persona.is_file():
                    shutil.copy2(code_persona, persona_dst)
                elif isinstance(persona_data, dict):
                    atomic_write_json(persona_dst, persona_data, backup=False)
        elif code_persona.is_file() and not persona_dst.exists():
            shutil.copy2(code_persona, persona_dst)
    except Exception as exc:
        print(f"· Не удалось инициализировать j_persona.json: {exc}")
    print(f"✓ Стартовые файлы данных готовы в {d}")

    try:
        import discord  # noqa: F401
        print("✓ Библиотека discord.py установлена.")
    except ImportError:
        print("· discord.py не установлена (нужна только для Discord-бота): python -m pip install -U discord.py")

    node = shutil.which("node")
    npm = shutil.which("npm")
    if node and (CODE_DIR / "package.json").is_file() and not (CODE_DIR / "node_modules").is_dir():
        run_npm = yes
        if not yes:
            try:
                ans = input("Пакеты Minecraft-бота не установлены (нет node_modules). Выполнить npm install? [y/N] ")
                run_npm = ans.strip().lower() in ("y", "yes", "д", "да")
            except (EOFError, KeyboardInterrupt):
                run_npm = False
        if run_npm:
            if npm:
                print("Устанавливаю пакеты Minecraft-бота (npm install)…")
                rc = subprocess.call([npm, "install"], cwd=CODE_DIR)
                print("✓ Пакеты Minecraft-бота установлены." if rc == 0 else f"✗ npm install завершился с кодом {rc}.")
            else:
                print("· npm не найден — выполни вручную: npm install")

    from j_filelink import find_gmod_data
    src_addon = CODE_DIR / "addons" / "j_gmod"
    gm = find_gmod_data()
    if gm and src_addon.is_dir():
        dst_addon = gm.parent / "addons" / "j_gmod"
        try:
            if src_addon.resolve() != dst_addon.resolve():
                dst_addon.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(src_addon, dst_addon, dirs_exist_ok=True)
            print(f"✓ Аддон Garry's Mod установлен/обновлён: {dst_addon}")
        except OSError as exc:
            print(f"✗ Не удалось установить аддон Garry's Mod: {exc}")
    elif gm:
        print(f"· Папка аддона не найдена ({src_addon}) — установка в GMod пропущена.")

    print()
    return _doctor()


def cmd_start(arg: str | None, window: bool) -> int:
    code = 0
    v = effective_values()
    is_all = not arg or arg == "all"
    force_offline = (arg == "offline")
    offline_mode = force_offline or (v.get("J_OFFLINE_MODE", "0").strip().lower() in ("1", "true", "yes", "on", "да"))
    extra_env = {"J_OFFLINE_MODE": "1"} if force_offline else None
    started_bots = 0
    skipped_offline = 0
    for name in targets(arg):
        if is_all and offline_mode and name in ("discord", "telegram", "whatsapp"):
            if (name == "discord" and v.get("DISCORD_TOKEN")) or (name == "telegram" and v.get("TELEGRAM_TOKEN")) or (name == "whatsapp" and v.get("WHATSAPP_TOKEN") and v.get("WHATSAPP_PHONE_ID")):
                skipped_offline += 1
            continue
        if is_all and name == "telegram" and not v.get("TELEGRAM_TOKEN"):
            continue
        if is_all and name == "whatsapp" and (not v.get("WHATSAPP_TOKEN") or not v.get("WHATSAPP_PHONE_ID")):
            continue
        if is_all and name == "discord" and not v.get("DISCORD_TOKEN"):
            continue
        if name in ("discord", "telegram", "whatsapp") and "core" in targets(arg) and not is_running("core") and not _core_up():
            print("Сначала ядро…")
        ok, msg = start_component(name, window, extra_env=extra_env if name == "core" else None)
        print(("✓ " if ok else "✗ ") + msg)
        code |= 0 if ok else 1
        if ok and name in ("discord", "telegram", "whatsapp"):
            started_bots += 1
        if name == "core" and ok:
            print("  жду, пока ядро ответит…", end=" ", flush=True)
            up = wait_core()
            print("готово" if up else "НЕ ответило за 25 секунд — смотри журнал: python j_launcher.py logs core")
            code |= 0 if up else 1
    if is_all and code == 0 and offline_mode and skipped_offline > 0:
        print("· Режим офлайн (J_OFFLINE_MODE=1): облачные боты (Discord/Telegram/WhatsApp) пропущены, ядро запущено автономно.")
    elif is_all and code == 0 and started_bots == 0:
        print("· Токены ботов (Discord/Telegram/WhatsApp) не заданы — ядро запущено автономно.")
    return code


def _core_up() -> bool:
    from j_core import is_core_running
    return bool(is_core_running())


def cmd_logs(name: str | None, lines: int) -> int:
    if name not in COMPONENTS:
        print(f"Укажи компонент: {', '.join(COMPONENTS)}")
        return 1
    p = log_path(name)
    if not p.exists():
        print("Журнала пока нет (компонент ещё не запускался через j_launcher.py или работает в своём окне).")
        return 0
    text = p.read_text(encoding="utf-8", errors="replace").splitlines()
    print("\n".join(text[-lines:]))
    return 0


def cmd_backup() -> int:
    dest, notes = make_backup()
    for n in notes:
        print("·", n)
    print("Копия сохранена:", dest.name if dest else "не получилось")
    print("Всего копий:", ", ".join(p.name for p in list_backups()[-5:]) or "нет")
    return 0 if dest else 1


def cmd_restore(name: str | None, yes: bool) -> int:
    backups = list_backups()
    if not name:
        print("Доступные копии (новые внизу):")
        for p in backups:
            print("  ", p.name)
        print("Вернуть: python j_launcher.py restore ИМЯ")
        return 0
    if is_running("core") or _core_up():
        print("Сначала останови ядро: python j_launcher.py stop core (иначе оно перезапишет вернувшиеся файлы).")
        return 1
    if not yes:
        if input(f"Вернуть память из копии {name}? Текущие файлы сохранятся с суффиксом .before-restore. [y/N] ").strip().lower() != "y":
            print("Отмена.")
            return 0
    try:
        done = restore(name)
    except FileNotFoundError as exc:
        print(exc)
        return 1
    print("Возвращено:", ", ".join(done) or "ничего")
    return 0


def cmd_env() -> int:
    values = effective_values()
    for key, opt in BY_KEY.items():
        value = values.get(key, "")
        shown = mask(key, value) if value else f"({opt.default or 'не задано'})"
        print(f"{key:20} {shown}")
    return 0


def cmd_test() -> int:
    return subprocess.call([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=CODE_DIR)


def menu() -> int:
    items = [("Первичная установка", lambda: cmd_install(False)), ("Диагностика", lambda: _doctor()),
             ("Запустить всё (ядро + боты при наличии токенов)", lambda: cmd_start("all", False)),
             ("Запустить автономно без интернета (ядро, офлайн)", lambda: cmd_start("offline", False)),
             ("Запустить только ядро", lambda: cmd_start("core", False)), ("Запустить голос", lambda: cmd_start("voice", True)),
             ("Остановить всё", lambda: [print(stop_component(n)) for n in ("voice", "whatsapp", "telegram", "discord", "core")] and 0),
             ("Статус", lambda: print("\n".join(status_lines())) or 0),
             ("Журнал ядра", lambda: cmd_logs("core", 30)), ("Резервная копия", cmd_backup), ("Тесты", cmd_test)]
    while True:
        print("\n=== J: управление ===")
        for i, (title, _) in enumerate(items, 1):
            print(f" {i}) {title}")
        print(" 0) Выход")
        try:
            choice = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if choice in ("0", "q", ""):
            return 0
        if choice.isdigit() and 1 <= int(choice) <= len(items):
            print()
            items[int(choice) - 1][1]()
        else:
            print("Введи номер из списка.")


def _doctor() -> int:
    from j_doctor import main as doctor_main
    return doctor_main()


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    argv = list(sys.argv[1:] if argv is None else argv)
    window = "--window" in argv
    yes = "--yes" in argv
    argv = [a for a in argv if not a.startswith("--")]
    load_env()
    cmd = argv[0] if argv else ""
    arg = argv[1] if len(argv) > 1 else None
    if not cmd:
        return menu()
    if cmd == "install":
        return cmd_install(yes)
    if cmd == "doctor":
        return _doctor()
    if cmd == "init":
        return cmd_init()
    if cmd == "start":
        return cmd_start(arg, window)
    if cmd == "stop":
        names = list(reversed(ORDER + ["voice"])) if not arg or arg == "all" else targets(arg)
        for n in names:
            print(stop_component(n))
        return 0
    if cmd == "status":
        print("\n".join(status_lines()))
        return 0
    if cmd == "logs":
        return cmd_logs(arg, int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else 40)
    if cmd == "backup":
        return cmd_backup()
    if cmd == "restore":
        return cmd_restore(arg, yes)
    if cmd == "env":
        return cmd_env()
    if cmd == "test":
        return cmd_test()
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
