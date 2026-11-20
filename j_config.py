#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Все настройки J в одном описании: что за параметр, значение по умолчанию, какого он типа.

Зачем:
  • проверка j.env на опечатки («J_DEVELOPER» вместо «J_DEVELOPERS») и неверные значения;
  • автоматический пример j.env.example со всеми параметрами и пояснениями;
  • j_doctor.py и j_launcher.py берут описания отсюда.

Сами значения модули по-прежнему читают из переменных среды (j.env загружается в них при запуске).
Новый параметр = одна строка в OPTS.
"""
from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass
from pathlib import Path

BASE = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Opt:
    key: str
    default: str
    kind: str          # str | int | float | bool | path | dir | csv | ids
    desc: str
    section: str
    secret: bool = False


OPTS: list[Opt] = [
    # --- ядро
    Opt("J_PORT", "8765", "int", "Порт ядра на 127.0.0.1 (меняй, только если занят)", "Ядро"),
    Opt("J_DATA_DIR", "", "dir", "Папка для ВСЕХ данных J (память, знания, копии, журналы). Пусто = рядом с кодом. "
        "Лучше вне OneDrive, например C:\\JData", "Ядро"),
    Opt("J_BACKUP_HOURS", "12", "float", "Как часто делать резервную копию памяти, часов (0 = выключить)", "Ядро"),
    Opt("J_BACKUP_KEEP", "14", "int", "Сколько копий хранить", "Ядро"),
    Opt("J_SPAM_LIMIT", "15", "int", "Лимит сообщений за 10 секунд для антиспама", "Ядро"),
    # --- люди и права
    Opt("J_OWNERS", "", "ids", "Владельцы: могут одобрять знания и менять общую память. Discord ID через запятую", "Люди"),
    Opt("J_DEVELOPERS", "", "ids", "Разработчики (уважение J 100 %): Discord ID, mc:Ник, gm:SteamID64, ro:Имя, l4:… "
        "через запятую", "Люди"),
    Opt("J_USER_LINKS", "", "str", "Связки профилей: discord_id=mc:Ник=gm:SteamID64;…", "Люди"),
    Opt("J_NOTES_OPEN", "1", "bool", "1 — общие заметки !note может добавлять любой, 0 — только доверенные", "Люди"),
    Opt("J_RESPECT_DECAY", "1", "float", "На сколько пунктов в день «остывает» уважение без общения", "Люди"),
    # --- Discord
    Opt("DISCORD_TOKEN", "", "str", "Токен Discord-бота (Developer Portal → Bot). Никому не показывай", "Discord", secret=True),
    Opt("J_CHANNELS", "", "csv", "Каналы, где J отвечает на любые сообщения (имена через запятую)", "Discord"),
    Opt("J_CORE_URL", "http://127.0.0.1:8765", "str", "Где живёт ядро (для Discord и голоса)", "Discord"),
    Opt("J_DISCORD_EMBEDDED", "0", "bool", "1 — Discord-бот со своим отдельным мозгом (старый режим, на случай проблем)", "Discord"),
    # --- Telegram
    Opt("TELEGRAM_TOKEN", "", "str", "Токен Telegram-бота (от @BotFather). Никому не показывай", "Telegram", secret=True),
    Opt("J_TELEGRAM_ALLOWED", "", "csv", "Разрешённые Telegram ID или @username через запятую (пусто = отвечает всем)", "Telegram"),
    Opt("J_TELEGRAM_EMBEDDED", "0", "bool", "1 — Telegram-бот со своим отдельным мозгом (если ядро не запущено)", "Telegram"),
    # --- WhatsApp
    Opt("WHATSAPP_TOKEN", "", "str", "Токен доступа WhatsApp Cloud API (Meta Graph API). Никому не показывай", "WhatsApp", secret=True),
    Opt("WHATSAPP_PHONE_ID", "", "str", "Phone Number ID из Meta Developers (WhatsApp → API Setup)", "WhatsApp"),
    Opt("WHATSAPP_VERIFY_TOKEN", "", "str", "Секретное слово для подтверждения webhook в Meta (hub.verify_token)", "WhatsApp"),
    Opt("WHATSAPP_APP_SECRET", "", "str", "App Secret для проверки подписи X-Hub-Signature-256 (необязательно)", "WhatsApp", secret=True),
    Opt("WHATSAPP_PORT", "8788", "int", "Локальный порт webhook-сервера WhatsApp (для ngrok / туннеля)", "WhatsApp"),
    Opt("J_WHATSAPP_ALLOWED", "", "csv", "Разрешённые номера WhatsApp через запятую (пусто = отвечает всем)", "WhatsApp"),
    Opt("J_WHATSAPP_EMBEDDED", "0", "bool", "1 — WhatsApp-бот со своим отдельным мозгом (если ядро не запущено)", "WhatsApp"),
    # --- обучение
    Opt("J_STUDY_MINUTES", "30", "float", "Как часто J изучает очередную тему из очереди, минут (0 = выключить)", "Обучение"),
    Opt("J_AUTO_TOPICS", "1", "bool", "1 — J сама записывает темы из вопросов без ответа", "Обучение"),
    Opt("J_CONTACT", "", "str", "Твой ник или почта: указывается в запросах к вики (по правилам вики)", "Обучение"),
    Opt("J_LLM_PROVIDER", "off", "str", "Провайдер LLM: off, auto, local, ollama, openai, gemini", "Обучение"),
    Opt("J_LLM_URL", "", "str", "URL API LLM (например http://127.0.0.1:11434/api/chat)", "Обучение"),
    Opt("J_LLM_MODEL", "", "str", "Имя модели LLM", "Обучение"),
    Opt("J_LLM_KEY", "", "str", "Ключ API LLM", "Обучение", secret=True),
    # --- автономность
    Opt("J_AUTONOMOUS", "1", "bool", "1 — автономный приоритет (офлайн-вычисления, бортовой архив и FDIR работают всегда)", "Автономность"),
    Opt("J_OFFLINE_MODE", "0", "bool", "1 — строгий офлайн-режим (полный запрет внешних HTTP/LLM-запросов, только локальные модули)", "Автономность"),
    Opt("J_HOST", "127.0.0.1", "str", "Адрес прослушивания HTTP-сервера ядра (127.0.0.1 — только этот ПК; 0.0.0.0 или LAN IP — для связи роботов/агента N в локальной сети)", "Автономность"),
    Opt("J_AUTO_APPROVE", "0", "bool", "1 — автоматически одобрять найденные знания в автономном режиме", "Автономность"),
    Opt("J_NET_TIMEOUT", "10", "float", "Таймаут сетевых запросов (сек) перед переходом на автономный резерв", "Автономность"),
    Opt("J_TELEMETRY_DIR", "", "dir", "Папка файлового канала телеметрии и приказов (для роботов и симуляторов без HTTP)", "Автономность"),
    Opt("J_PEER_AGENTS", "N,Serial Designation N,SD-N,н,дрон н", "csv",
        "Имена или ID дружественных ИИ-агентов (например N / Serial Designation N / ID бота), которым J отвечает", "Автономность"),
    Opt("J_N_URL", "", "str", "HTTP-адрес второго ИИ-агента N (например http://127.0.0.1:8766) для прямой связи между агентами", "Автономность"),
    Opt("J_PEER_MAX_TURNS", "3", "int",
        "Максимум подряд ответов другому ИИ-агенту без участия человека (защита от бесконечного цикла)", "Автономность"),
    Opt("J_PEER_COOLDOWN", "4", "float", "Минимальная пауза в секундах между автоответами ИИ-агенту в общем чате", "Автономность"),
    # --- игры
    Opt("GMOD_DATA", "", "dir", "Папка garrysmod\\data (если ядро не нашло её само)", "Игры"),
    Opt("L4D2_DATA", "", "dir", "Папка обмена Left 4 Dead 2: …\\left4dead2\\ems (VScript) или …\\addons\\sourcemod\\data", "Игры"),
    Opt("MC_HOST", "localhost", "str", "Адрес сервера Minecraft (для бота j_bot.js)", "Игры"),
    Opt("MC_PORT", "25565", "int", "Порт сервера Minecraft", "Игры"),
    Opt("MC_USERNAME", "J", "str", "Ник бота в Minecraft", "Игры"),
    Opt("MC_VERSION", "", "str", "Версия Minecraft (пусто = определить автоматически), например 1.12.2", "Игры"),
    Opt("MC_AUTH", "offline", "str", "Режим входа в Minecraft: offline (пиратский/локальный) или microsoft (лицензия)", "Игры"),
    Opt("MC_SKIN", "", "str", "Скин бота для плагина SkinsRestorer (URL картинки .png или ник игрока; скин лежит в skins\\j_skin.png)", "Игры"),
    Opt("MC_OWNER", "", "str", "Ник игрока, который может приказывать боту в Minecraft", "Игры"),
    # --- голос
    Opt("J_VOICE_USER", "Игрок", "str", "Как J называет тебя в голосовом режиме (профиль ro:<имя>)", "Голос"),
    Opt("J_STT", "text", "str", "Режим ввода голоса: text (клавиатура) или vosk (микрофон)", "Голос"),
    Opt("J_VOSK_MODEL", "", "dir", "Папка модели распознавания речи Vosk", "Голос"),
    Opt("J_PTT_KEY", "F9", "str", "Кнопка «говорить»: F1–F12, A–Z, LCTRL, RCTRL, XBUTTON1, XBUTTON2…", "Голос"),
    Opt("J_VOICE_MODE", "ptt", "str", "Режим микрофона для vosk: ptt (по кнопке J_PTT_KEY), vad (по голосу), wake (по слову «Джей»)", "Голос"),
    Opt("J_WAKE_WORDS", "джей,j,эй джей,слушай джей", "csv", "Ключевые слова активации голосом в режиме wake", "Голос"),
    Opt("J_VOICE_GAME", "ron", "str", "Профиль клиента для голосового помощника (ron, gmod, minecraft, robot…)", "Голос"),
    Opt("J_TTS_RATE", "0", "int", "Скорость речи J, от -10 до 10", "Голос"),
    Opt("J_TTS_VOLUME", "100", "int", "Громкость речи J, 0–100", "Голос"),
    Opt("J_VOICE_PYTHON", "", "path", "Python, где стоят vosk и sounddevice (для запуска голоса из j_launcher.py), "
        "например C:\\jvoice\\Scripts\\python.exe", "Голос"),
    Opt("J_VOICE_BRIDGE", "", "str", "УСТАРЕЛО: используй J_CORE_URL", "Голос"),
    # --- пути файлов по отдельности (необязательно)
    Opt("J_MEMORY", "", "path", "Отдельный путь к памяти J (по умолчанию J_DATA_DIR\\j_memory.json)", "Файлы"),
    Opt("J_SELF", "", "path", "Отдельный путь к самомодели", "Файлы"),
    Opt("J_KNOWLEDGE", "", "path", "Отдельный путь к знаниям", "Файлы"),
    Opt("J_NOTES", "", "path", "Отдельный путь к общим заметкам", "Файлы"),
    Opt("J_POLICY", "", "path", "Отдельный путь к правилам j_policy.json", "Файлы"),
    Opt("J_SOURCES", "", "path", "Отдельный путь к списку источников j_sources.json", "Файлы"),
    Opt("J_PERSONA", "", "path", "Отдельный путь к файлу характера j_persona.json", "Файлы"),
    Opt("J_PLACES", "", "path", "Отдельный путь к файлу мест j_places.json", "Файлы"),
    Opt("J_ENV", "", "path", "Путь к файлу настроек (по умолчанию j.env рядом с кодом; задаётся только переменной среды)", "Файлы"),
]
BY_KEY = {o.key: o for o in OPTS}
TRUE = {"1", "true", "yes", "on", "да", "y"}
FALSE = {"0", "false", "no", "off", "нет", "n", ""}
ID_RE = re.compile(r"^(\d{15,22}|(mc|gm|ro|l4|rb|as|dr|tg|wa|ag):\S+)$", re.I)


@dataclass
class Issue:
    level: str   # ERROR | WARN | INFO
    key: str
    message: str


def parse_env_file(path: Path) -> tuple[dict[str, str], list[Issue]]:
    """Читает j.env: значения и замечания по строкам (без значений секретов в тексте замечаний)."""
    values: dict[str, str] = {}
    issues: list[Issue] = []
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return values, [Issue("INFO", "", f"файл {Path(path).name} не найден (настройки по умолчанию)")]
    except OSError as exc:
        return values, [Issue("ERROR", "", f"не удалось прочитать {Path(path).name}: {exc}")]
    for n, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            issues.append(Issue("WARN", f"строка {n}", "нет знака «=» — строка будет проигнорирована"))
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key in values:
            issues.append(Issue("WARN", key, f"задан повторно (строка {n}): действует первое значение"))
            continue
        values[key] = val
    return values, issues


def validate(values: dict[str, str]) -> list[Issue]:
    """Проверка набора значений (из файла или среды) по описанию параметров."""
    out: list[Issue] = []
    for key, val in values.items():
        opt = BY_KEY.get(key)
        if opt is None:
            if key.startswith(("J_", "MC_", "GMOD_", "L4D2_")) or key == "DISCORD_TOKEN":
                near = difflib.get_close_matches(key, list(BY_KEY), n=1, cutoff=0.7)
                hint = f" Возможно, ты имел в виду {near[0]}?" if near else ""
                out.append(Issue("WARN", key, "неизвестная настройка (опечатка?)." + hint))
            continue
        v = val.strip()
        if not v:
            continue
        if opt.kind == "int":
            try:
                int(v)
            except ValueError:
                out.append(Issue("ERROR", key, f"ожидалось целое число, а не «{v}»"))
        elif opt.kind == "float":
            try:
                float(v)
            except ValueError:
                out.append(Issue("ERROR", key, f"ожидалось число, а не «{v}»"))
        elif opt.kind == "bool" and v.lower() not in TRUE | FALSE:
            out.append(Issue("ERROR", key, f"ожидалось 1 или 0, а не «{v}»"))
        elif opt.kind == "ids":
            for part in [p.strip() for p in v.split(",") if p.strip()]:
                if not ID_RE.match(part):
                    out.append(Issue("WARN", key, f"«{part}» не похоже на ID: нужен Discord ID (число из 15–22 цифр) "
                                                  "или mc:/gm:/ro:/l4:/rb:/as:/dr:/tg:/wa:<идентификатор>"))
        elif opt.kind == "dir" and key != "J_DATA_DIR" and not Path(v).is_dir():
            out.append(Issue("WARN", key, f"папка не найдена: {v}"))
        elif opt.kind == "path" and key.endswith("PYTHON") and not Path(v).is_file():
            out.append(Issue("WARN", key, f"файл не найден: {v}"))
        if key == "DISCORD_TOKEN" and len(v) < 50:
            out.append(Issue("WARN", key, "токен выглядит слишком коротким (обычно около 70 символов): не обрезан ли он при копировании?"))
        if key == "J_STT" and v.lower() not in ("text", "vosk"):
            out.append(Issue("ERROR", key, "допустимо text или vosk"))
        if key == "J_LLM_PROVIDER" and v.lower() not in ("off", "auto", "local", "ollama", "openai", "gemini"):
            out.append(Issue("ERROR", key, "допустимо off, auto, local, ollama, openai или gemini"))
        if key == "J_VOICE_BRIDGE":
            out.append(Issue("INFO", key, "устаревшая настройка: используй J_CORE_URL"))
    return out


def mask(key: str, value: str) -> str:
    opt = BY_KEY.get(key)
    if opt and opt.secret and value:
        return value[:4] + "…" + f"({len(value)} симв.)"
    return value


def render_example() -> str:
    """Текст j.env.example: все параметры по разделам, значения по умолчанию закомментированы."""
    lines = ["# j.env — настройки J. Скопируй этот файл как j.env и раскомментируй нужные строки.",
             "# Строки с # игнорируются. Пробелы вокруг = допустимы, кавычки не нужны.", ""]
    section = None
    for o in OPTS:
        if o.section != section:
            section = o.section
            lines += ["", f"# ===== {section} ====="]
        lines.append(f"# {o.desc}")
        lines.append(f"# {o.key}={o.default}")
    return "\n".join(lines) + "\n"


def effective_values(env_file: Path | None = None) -> dict[str, str]:
    """Значения, которые реально увидят модули: переменные среды поверх j.env."""
    path = Path(env_file) if env_file else Path(os.environ.get("J_ENV") or BASE / "j.env")
    file_values, _ = parse_env_file(path)
    merged = dict(file_values)
    for key in BY_KEY:
        if os.environ.get(key):
            merged[key] = os.environ[key]
    return merged
