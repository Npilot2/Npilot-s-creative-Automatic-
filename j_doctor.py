#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Диагностика J: «что у меня не так?»

Запуск:  python j_launcher.py doctor     (или python j_doctor.py)
Проверяет файлы проекта, версию Python, настройки j.env, папку данных, память, порт ядра, Discord, игры и голос.
Секреты (токен) никогда не печатаются. Каждое замечание сопровождается тем, что делать.
Уровни: OK — всё хорошо · INFO — к сведению · WARN — стоит поправить · ERROR — так работать не будет.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from j_backup import DATA_FILES, latest_age_hours
from j_config import BY_KEY, effective_values, parse_env_file, validate
from j_paths import CODE_DIR, data_dir, data_path

REQUIRED = ["j_ai", "j_policy", "j_nlu", "j_web", "j_self", "j_discord", "j_filelink", "j_games", "j_bridge", "j_core",
            "j_config", "j_paths", "j_backup", "j_mc_bridge", "j_launcher"]
OPTIONAL = ["j_voice.py", "j_bot.js", "package.json", "jokes.json"]


@dataclass
class Check:
    level: str   # OK | INFO | WARN | ERROR
    title: str
    detail: str = ""
    fix: str = ""


def _env_path() -> Path:
    return Path(os.environ.get("J_ENV") or CODE_DIR / "j.env")


def check_python() -> list[Check]:
    v = sys.version_info
    bits = "64" if sys.maxsize > 2**32 else "32"
    if v < (3, 9):
        return [Check("ERROR", f"Python {v.major}.{v.minor} слишком старый", "нужен 3.9 или новее", "поставь свежий Python с python.org")]
    return [Check("OK", f"Python {v.major}.{v.minor}.{v.micro} ({bits}-бит)", sys.executable)]


def check_files() -> list[Check]:
    out: list[Check] = []
    missing = [m + ".py" for m in REQUIRED if not (CODE_DIR / (m + ".py")).is_file()]
    if missing:
        out.append(Check("ERROR", "Не хватает файлов проекта", ", ".join(missing), "распакуй архив обновления поверх папки J целиком"))
    else:
        out.append(Check("OK", "Все файлы проекта на месте"))
    absent = [f for f in OPTIONAL if not (CODE_DIR / f).exists()]
    if absent:
        out.append(Check("INFO", "Необязательные файлы отсутствуют", ", ".join(absent), "нужны только для голоса, Minecraft и шуток"))
    if "onedrive" in str(CODE_DIR).lower():
        out.append(Check("WARN", "Проект лежит в OneDrive", str(CODE_DIR),
                         "OneDrive иногда блокирует файлы памяти при записи. Вынеси данные: J_DATA_DIR=C:\\JData в j.env"))
    return out


def check_env() -> list[Check]:
    path = _env_path()
    values, issues = parse_env_file(path)
    out: list[Check] = []
    if not path.exists():
        out.append(Check("INFO", "Файла j.env нет", str(path), "создай его командой: python j_launcher.py init"))
    else:
        out.append(Check("OK", f"j.env найден: {len(values)} настроек"))
    for i in issues:
        if i.level != "INFO":
            out.append(Check(i.level, f"j.env: {i.key}", i.message))
    for i in validate(effective_values(path)):
        out.append(Check(i.level, f"Настройка {i.key}", i.message,
                         "сверь с j.env.example (python j_launcher.py init создаст его)" if i.level == "WARN" else ""))
    return out


def check_data() -> list[Check]:
    out: list[Check] = []
    d = data_dir()
    probe = d / ".write_test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        out.append(Check("OK", f"Папка данных доступна для записи: {d}"))
    except OSError as exc:
        out.append(Check("ERROR", "Нельзя писать в папку данных", f"{d}: {exc}", "задай другую: J_DATA_DIR=C:\\JData"))
        return out
    for name, env in DATA_FILES:
        p = data_path(name, env)
        if not p.exists():
            continue
        try:
            json.loads(p.read_text(encoding="utf-8-sig"))
            out.append(Check("OK", f"{p.name}: читается ({p.stat().st_size // 1024 + 1} КБ)"))
        except (OSError, ValueError) as exc:
            out.append(Check("ERROR", f"{p.name} повреждён", str(exc),
                             "восстанови копию: python j_launcher.py restore (список копий: python j_launcher.py backup)"))
    age = latest_age_hours()
    if age is None:
        out.append(Check("INFO", "Резервных копий пока нет", "", "первая сделается при запуске ядра или командой: python j_launcher.py backup"))
    else:
        level = "OK" if age < 48 else "WARN"
        out.append(Check(level, f"Последняя резервная копия {age:.0f} ч. назад",
                         "" if level == "OK" else "давно не было копий", "" if level == "OK" else "ядро делает их само, пока работает"))
    return out


def check_core() -> list[Check]:
    from j_core import host, is_core_running, port
    bind_h = host()
    info = is_core_running()
    if info:
        return [Check("OK", f"Ядро J работает (версия {info.get('version')}, аптайм {info.get('uptime')} с)", f"http://{bind_h}:{port()}")]
    s = socket.socket()
    try:
        s.bind((bind_h, port()))
        return [Check("INFO", "Ядро J не запущено", f"{bind_h}:{port()} свободен", "запусти: python j_launcher.py start core")]
    except OSError:
        return [Check("ERROR", f"Адрес {bind_h}:{port()} недоступен или порт занят", "проверь J_HOST / J_PORT", "задай другой J_HOST или J_PORT в j.env")]
    finally:
        s.close()


def check_discord() -> list[Check]:
    v = effective_values(_env_path())
    out: list[Check] = []
    token = v.get("DISCORD_TOKEN", "")
    out.append(Check("OK" if token else "INFO", "Токен Discord " + ("задан" if token else "не задан (Discord-бот не запустится)"),
                     "", "" if token else "DISCORD_TOKEN=… в j.env"))
    try:
        import discord  # noqa: F401
        out.append(Check("OK", "Библиотека discord.py установлена"))
    except ImportError:
        out.append(Check("INFO" if not token else "WARN", "Библиотека discord.py не установлена", "нужна только для Discord-бота",
                         "python -m pip install -U discord.py"))
    for key in ("J_OWNERS", "J_DEVELOPERS"):
        out.append(Check("OK" if v.get(key) else "INFO", f"{key} " + ("задан" if v.get(key) else "не задан"),
                         "" if v.get(key) else BY_KEY[key].desc))
    tg = v.get("TELEGRAM_TOKEN", "")
    out.append(Check("OK" if tg else "INFO", "Токен Telegram " + ("задан" if tg else "не задан (Telegram-бот не запустится)"),
                     "", "" if tg else "TELEGRAM_TOKEN=… в j.env"))
    wa = bool(v.get("WHATSAPP_TOKEN") and v.get("WHATSAPP_PHONE_ID"))
    out.append(Check("OK" if wa else "INFO", "WhatsApp " + ("настроен" if wa else "не настроен (WhatsApp-бот не запустится)"),
                     "", "" if wa else "WHATSAPP_TOKEN=… и WHATSAPP_PHONE_ID=… в j.env"))
    return out


def check_games() -> list[Check]:
    from j_filelink import find_gmod_data
    v = effective_values(_env_path())
    out: list[Check] = []
    gm = find_gmod_data()
    out.append(Check("OK" if gm else "INFO", "Garry's Mod: " + (f"папка data найдена ({gm})" if gm else "папка не найдена"),
                     "" if gm else "нужно только для GMod", "" if gm else "GMOD_DATA=…\\garrysmod\\data в j.env"))
    if gm:
        addon_dir = gm.parent / "addons" / "j_gmod"
        if addon_dir.is_dir():
            out.append(Check("OK", f"Аддон Garry's Mod j_gmod установлен ({addon_dir})"))
        else:
            out.append(Check("WARN", "Аддон Garry's Mod j_gmod не установлен", str(addon_dir),
                             "выполни: python j_launcher.py install"))
    l4 = v.get("L4D2_DATA", "")
    if l4:
        ok = Path(l4).is_dir()
        out.append(Check("OK" if ok else "WARN", f"Left 4 Dead 2: {'папка обмена найдена' if ok else 'папка не найдена'}", l4))
    node = shutil.which("node")
    out.append(Check("OK" if node else "INFO", "Node.js (для Minecraft-бота): " + ("найден" if node else "не найден"),
                     "" if node else "нужен только для Minecraft", "" if node else "поставь LTS с nodejs.org"))
    if node:
        has_mods = (CODE_DIR / "node_modules").is_dir()
        out.append(Check("OK" if has_mods else "INFO", "Пакеты Minecraft-бота " + ("установлены" if has_mods else "не установлены"),
                         "" if has_mods else "", "" if has_mods else "в папке J выполни: npm install"))
    return out


def check_voice() -> list[Check]:
    v = effective_values(_env_path())
    out: list[Check] = []
    py = v.get("J_VOICE_PYTHON", "")
    if py:
        if not Path(py).is_file():
            out.append(Check("WARN", "J_VOICE_PYTHON указывает на несуществующий файл", py))
        else:
            try:
                r = subprocess.run([py, "-c", "import vosk, sounddevice"], capture_output=True, text=True, timeout=30)
                out.append(Check("OK" if r.returncode == 0 else "WARN", "vosk и sounddevice в голосовом Python: " +
                                 ("есть" if r.returncode == 0 else "не найдены"), "" if r.returncode == 0 else r.stderr.strip()[-200:],
                                 "" if r.returncode == 0 else f"{py} -m pip install vosk sounddevice"))
            except (OSError, subprocess.SubprocessError) as exc:
                out.append(Check("WARN", "Не удалось проверить голосовой Python", str(exc)))
    else:
        out.append(Check("INFO", "J_VOICE_PYTHON не задан", "нужен только для запуска голоса через j_launcher.py"))
    if os.name == "nt":
        try:
            from j_voice import Speaker
            ru = Speaker.russian_voice_installed()
            if ru is False:
                out.append(Check("WARN", "В Windows нет русского голоса", "J будет читать по-русски плохо",
                                 "Параметры → Время и язык → Речь → Добавить голоса"))
            elif ru:
                out.append(Check("OK", "Русский голос Windows установлен"))
        except Exception:
            pass
    return out


def check_policy() -> list[Check]:
    p = data_path("j_policy.json", "J_POLICY")
    if not p.exists():
        return [Check("INFO", "Файл правил j_policy.json ещё не создан", "", "создастся при первом запуске ядра")]
    try:
        json.loads(p.read_text(encoding="utf-8-sig"))
        return [Check("OK", "Файл правил j_policy.json корректен")]
    except (OSError, ValueError) as exc:
        return [Check("ERROR", "Ошибка в j_policy.json", str(exc), "поправь указанную строку; пока J использует стандартные правила")]


def check_persona() -> list[Check]:
    p = data_path("j_persona.json", "J_PERSONA")
    try:
        import j_ai
        persona_err = getattr(j_ai, "PERSONA_ERROR", "")
    except Exception:
        persona_err = ""
    if not p.exists():
        if persona_err:
            return [Check("ERROR", "Ошибка в j_persona.json", persona_err,
                          "поправь синтаксис JSON или выполни: python j_launcher.py install")]
        code_p = CODE_DIR / "j_persona.json"
        if code_p.is_file():
            try:
                data = json.loads(code_p.read_text(encoding="utf-8-sig"))
                if not isinstance(data, dict):
                    return [Check("ERROR", "Ошибка в j_persona.json", "ожидался JSON-объект")]
                return [Check("OK", "Файл характера j_persona.json корректен (в папке кода)")]
            except (OSError, ValueError) as exc:
                return [Check("ERROR", "Ошибка в j_persona.json", str(exc),
                              "поправь JSON в j_persona.json; пока J использует стандартный характер")]
        return [Check("INFO", "Файл характера j_persona.json ещё не создан", "",
                      "создастся при первом запуске или командой: python j_launcher.py install")]
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            return [Check("ERROR", "Ошибка в j_persona.json", "ожидался JSON-объект")]
    except (OSError, ValueError) as exc:
        return [Check("ERROR", "Ошибка в j_persona.json", str(exc),
                      "поправь указанную строку или восстанови копию; пока J использует стандартный характер")]
    if persona_err:
        return [Check("ERROR", "Ошибка в j_persona.json", persona_err,
                      "поправь синтаксис JSON; пока J использует стандартный характер")]
    return [Check("OK", "Файл характера j_persona.json корректен")]


def check_autonomy() -> list[Check]:
    """Проверяет настройки автономности, файлового канала телеметрии и бортовой офлайн-архив."""
    v = effective_values(_env_path())
    out: list[Check] = []
    raw_auto = (v.get("J_AUTONOMOUS") or BY_KEY["J_AUTONOMOUS"].default).strip().lower()
    autonomous = raw_auto not in ("0", "false", "no", "off", "нет")
    raw_offline = (v.get("J_OFFLINE_MODE") or BY_KEY["J_OFFLINE_MODE"].default).strip().lower()
    offline_mode = raw_offline in ("1", "true", "yes", "on", "да")
    if offline_mode:
        out.append(Check("OK", "Автономный режим: строгий офлайн (J_OFFLINE_MODE=1)",
                         "внешние HTTP/LLM-запросы отключены; работают локальный мозг, бортовой архив и FDIR"))
    elif autonomous:
        out.append(Check("OK", "Автономный режим: гибридный с автономным приоритетом (J_AUTONOMOUS=1)",
                         "офлайн-вычисления, бортовой архив и FDIR активны всегда; сеть используется при наличии"))
    else:
        out.append(Check("INFO", "Автономный приоритет выключен (J_AUTONOMOUS=0)",
                         "резервные офлайн-модули остаются доступны"))

    from j_ai import is_local_or_lan_url
    prov = (v.get("J_LLM_PROVIDER") or BY_KEY["J_LLM_PROVIDER"].default).strip().lower()
    llm_url = (v.get("J_LLM_URL") or "").strip()
    if prov not in ("off", "auto", "local", "ollama", "openai", "gemini"):
        out.append(Check("ERROR", f"Некорректный J_LLM_PROVIDER: {prov}",
                         "допустимо off, auto, local, ollama, openai или gemini"))
    elif offline_mode and prov in ("openai", "gemini"):
        if is_local_or_lan_url(llm_url):
            out.append(Check("OK", f"Провайдер LLM: {prov} (локальный/LAN URL)",
                             "доступен в строгом офлайн-режиме без интернета"))
        else:
            out.append(Check("INFO", f"Провайдер LLM: {prov} (ограничен J_OFFLINE_MODE=1)",
                             "в строгом офлайн-режиме ответы формируются локально без сети"))
    elif prov in ("off", "auto", "local", "ollama"):
        out.append(Check("OK", f"Провайдер LLM: {prov}", "локально-совместимый режим без обязательного облака"))
    else:
        has_key = bool(v.get("J_LLM_KEY", "").strip())
        out.append(Check("OK" if has_key else "INFO",
                         f"Провайдер LLM: {prov}" + (" (ключ задан)" if has_key else " (без ключа — сработает локальный резерв)")))

    bind_h = (v.get("J_HOST") or BY_KEY["J_HOST"].default).strip() or "127.0.0.1"
    if bind_h not in ("127.0.0.1", "localhost", "::1"):
        out.append(Check("OK", f"Сетевой интерфейс ядра (J_HOST): {bind_h}",
                         "ядро доступно в локальной сети для роботов и агента N"))

    n_url = (v.get("J_N_URL") or "").strip()
    if n_url:
        if is_local_or_lan_url(n_url):
            out.append(Check("OK", f"Адрес агента N (J_N_URL): {n_url}",
                             "локальный/LAN адрес работает и в офлайне"))
        else:
            note = "блокируется при J_OFFLINE_MODE=1" if offline_mode else "требует доступ к внешнему узлу"
            out.append(Check("INFO", f"Адрес агента N (J_N_URL): {n_url}", note))

    tdir = v.get("J_TELEMETRY_DIR", "").strip().strip('"')
    if tdir:
        tp = Path(tdir)
        out.append(Check("OK" if tp.is_dir() else "WARN",
                         f"Файловый канал телеметрии: {'папка найдена' if tp.is_dir() else 'папка не найдена'}",
                         str(tp), "" if tp.is_dir() else "создай папку или поправь J_TELEMETRY_DIR в j.env"))
    else:
        out.append(Check("INFO", "Файловый канал телеметрии (J_TELEMETRY_DIR) не задан",
                         "телеметрия доступна по HTTP POST /telemetry и через Bridge.telemetry()"))

    try:
        import j_web
        builtin_kb = getattr(j_web, "BUILTIN_AEROSPACE_KB", None)
        if isinstance(builtin_kb, (list, tuple)) and builtin_kb:
            out.append(Check("OK", f"Бортовой офлайн-архив (аэрокосмос/робототехника): {len(builtin_kb)} статей"))
        else:
            out.append(Check("INFO", "Бортовой офлайн-архив (BUILTIN_AEROSPACE_KB) не найден в j_web"))
    except Exception as exc:
        out.append(Check("INFO", "Не удалось проверить бортовой офлайн-архив j_web", str(exc)))
    return out


ALL_CHECKS = [check_python, check_files, check_env, check_data, check_policy, check_persona, check_autonomy,
              check_core, check_discord, check_games, check_voice]


def run_checks() -> list[Check]:
    out: list[Check] = []
    for fn in ALL_CHECKS:
        try:
            out.extend(fn())
        except Exception as exc:  # диагностика сама не должна падать
            out.append(Check("WARN", f"Проверка {fn.__name__} не выполнилась", str(exc)))
    return out


def format_report(checks: list[Check]) -> str:
    mark = {"OK": "[ OK ]", "INFO": "[ .. ]", "WARN": "[ !! ]", "ERROR": "[ XX ]"}
    lines = ["Диагностика J", "=" * 60]
    for c in checks:
        lines.append(f"{mark[c.level]} {c.title}")
        if c.detail:
            lines.append(f"       {c.detail}")
        if c.fix:
            lines.append(f"       → {c.fix}")
    errors = sum(c.level == "ERROR" for c in checks)
    warns = sum(c.level == "WARN" for c in checks)
    lines += ["=" * 60, f"Итог: ошибок {errors}, предупреждений {warns}."
              + (" Исправь ошибки — иначе J работать не будет." if errors else " Критичного нет." if not warns else " Стоит поправить предупреждения.")]
    return "\n".join(lines)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    from j_discord import load_env
    load_env()
    checks = run_checks()
    print(format_report(checks))
    return 1 if any(c.level == "ERROR" for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
