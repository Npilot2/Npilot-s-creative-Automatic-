#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Оптимизированная копия ядра J (Serial Designation J, Murder Drones)."""
from __future__ import annotations

import ast
import copy
import functools
import ipaddress
import json
import math
import operator
import os
import random
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
RES_DIR = BASE if (BASE / "j_persona.json").is_file() else BASE.parent
if str(RES_DIR) not in sys.path:
    sys.path.insert(0, str(RES_DIR))

import j_paths
from j_paths import data_dir, data_path

MEMORY_FILE = BASE / "j_memory.json"
JOKES_FILE = RES_DIR / "jokes.json"
LEXICON_FILE = RES_DIR / "j_lexicon.json"
PERSONA_FILE = RES_DIR / "j_persona.json"
SEED_VERSION = 3
PERSONA_ERROR: str = ""
EXIT_WORDS = {"назад", "выход", "стоп", "отмена", "back", "exit", "stop", "cancel", "quit"}
ALPHA = 0.02
_WORD = re.compile(r"[\w']+", re.UNICODE)


def _atomic_write_json(path: Path, data, backup: bool = True, indent: int = 1) -> bool:
    fn = getattr(j_paths, "atomic_write_json", None)
    if callable(fn):
        return bool(fn(path, data, backup=backup, indent=indent))
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=indent), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def norm(text: str) -> str:
    return (text or "").strip().lower().replace("ё", "е")


@functools.lru_cache(maxsize=4096)
def _cached_stems(norm_text: str) -> tuple[str, ...]:
    return tuple(w[:5] for w in _WORD.findall(norm_text))


def stems(text: str) -> list[str]:
    return list(_cached_stems(norm(text)))


def jaccard(a: str, b: str) -> float:
    sa, sb = set(_cached_stems(norm(a))), set(_cached_stems(norm(b)))
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


SEEDS = {
    "greet": ["привет", "здравствуй", "хай", "йо", "добрый день", "доброе утро", "здорово", "приветствую", "салют",
              "hello", "hi", "hey", "good morning", "yo", "sup"],
    "bye": ["пока", "до свидания", "выход", "увидимся", "я пошел", "выйти", "прощай", "спокойной ночи", "бывай",
            "bye", "goodbye", "exit", "quit", "see you", "i am leaving"],
    "help": ["помощь", "помоги", "что ты умеешь", "команды", "что можешь", "меню", "подскажи команды", "справка",
             "help", "what can you do", "commands", "menu"],
    "joke": ["анекдот", "шутка", "расскажи шутку", "расскажи анекдот", "пошути", "юмор", "развесели",
             "дружеский подкол", "ироничный комплимент", "пофлиртуй", "двусмысленная шутка",
             "joke", "tell me a joke", "make me laugh", "funny story", "humor"],
    "time": ["время", "который час", "сколько времени", "сколько сейчас времени", "текущее время", "подскажи время",
             "time", "what time is it", "current time", "clock"],
    "rps": ["камень ножницы бумага", "сыграем в камень ножницы бумага", "цу е фа", "рпс", "давай в рпс",
            "rps", "rock paper scissors", "play rock paper scissors"],
    "guess": ["угадай число", "сыграем в угадай число", "загадай число", "угадайка",
              "guess", "guess the number", "number game"],
    "quiz": ["викторина", "квиз", "задай вопрос", "проверь мои знания", "устроим аттестацию",
             "quiz", "trivia", "ask me a question", "test me"],
    "stats": ["статистика", "мои результаты", "мой счет", "личное дело", "покажи статистику", "мой профиль",
              "stats", "my stats", "scoreboard", "results"],
    "who": ["кто ты", "как тебя зовут", "представься", "ты кто", "расскажи о себе",
            "who are you", "what is your name", "introduce yourself"],
    "thanks": ["спасибо", "благодарю", "спасибо большое", "выручила", "thanks", "thank you", "thx"],
    "praise": ["ты молодец", "отлично", "хороший бот", "ты крутая", "ты умная", "круто получилось", "ты лучшая",
               "great job", "good bot", "you are awesome", "nice work", "well done"],
    "insult": ["ты тупая", "дура", "идиот", "тупой бот", "заткнись", "ненавижу тебя", "бесполезная",
               "stupid", "idiot", "shut up", "i hate you", "dumb bot", "useless"],
    "apology": ["извини", "прости", "мои извинения", "сорян", "виноват", "прошу прощения",
                "sorry", "apologies", "my bad", "i apologize"],
    "funny": ["смешно", "очень смешно", "это смешно", "ха ха", "хаха", "лол", "годная шутка", "зачетно",
              "good one", "lol", "haha", "that was funny", "funny"],
    "lame": ["не смешно", "несмешно", "скучно", "плохая шутка", "тупая шутка", "кринж",
             "lame", "not funny", "boring joke", "bad joke"],
    "mood": ["как дела", "как ты", "как настроение", "как поживаешь", "как твое самочувствие", "ты в порядке",
             "how are you", "how is it going", "what is up"],
    "order": ["следующий шаг", "что делать дальше", "какой следующий приказ", "что мне делать", "дай задание",
              "next step", "what to do next", "what is the next order", "what should i do"],
    "movement": ["как идти", "куда двигаться", "следующее движение", "какой следующий шаг", "иди за мной",
                 "how to move", "where to go", "next movement", "what is the next step"],
}
INTENT_ALIASES = {
    "joke": ["шутка", "анекдот", "joke"], "time": ["время", "time"],
    "rps": ["камень", "рпс", "rps", "игра"], "guess": ["угадай", "число", "guess"],
    "quiz": ["викторина", "квиз", "quiz"], "help": ["помощь", "help"],
    "greet": ["приветствие", "привет", "greet", "hello"], "bye": ["прощание", "пока", "bye"],
    "stats": ["статистика", "stats"], "who": ["кто", "who"], "mood": ["настроение", "mood"],
    "order": ["приказ", "order"], "movement": ["движение", "movement"],
}
ALIAS_TO_INTENT = {a: i for i, al in INTENT_ALIASES.items() for a in al}
_LEXICON_STOP_STEMS = {
    "как", "что", "кто", "где", "это", "или", "для", "про", "без", "при", "над", "под", "через", "еще",
    "the", "and", "for", "with", "from", "that", "this", "what", "how", "who", "are", "you", "not", "out",
}


class IntentModel:
    def __init__(self) -> None:
        self.tok: dict[str, Counter] = defaultdict(Counter)
        self.docs: Counter = Counter()
        self.vocab: set[str] = set()
        self.filtered_junk: bool = False

    def train(self, text: str, intent: str, weight: float = 1.0) -> None:
        tokens = stems(text)
        if not tokens:
            return
        self.docs[intent] += weight
        for t in tokens:
            self.tok[intent][t] += weight
            self.vocab.add(t)

    def predict(self, text: str) -> tuple[str | None, float]:
        all_tokens = stems(text)
        tokens = [t for t in all_tokens if t in self.vocab]
        if not tokens or not self.docs:
            return None, 0.0
        content_tokens = [t for t in all_tokens if t not in _LEXICON_STOP_STEMS] or all_tokens
        known = len(tokens) / max(1, len(content_tokens))
        total_docs = sum(self.docs.values())
        v = len(self.vocab)
        scores = {}
        for intent, n_docs in self.docs.items():
            total = sum(self.tok[intent].values())
            if total > 0 and n_docs > 0:
                scores[intent] = math.log(n_docs / total_docs) + sum(
                    math.log((self.tok[intent][t] + ALPHA) / (total + ALPHA * v)) for t in tokens
                )
        if not scores:
            return None, 0.0
        top = max(scores.values())
        exp = {k: math.exp(s - top) for k, s in scores.items()}
        best = max(exp, key=exp.get)
        conf = exp[best] / sum(exp.values())
        return best, conf * known if known < 0.5 else conf

    def dump(self) -> dict:
        return {"docs": dict(self.docs), "tok": {k: dict(v) for k, v in self.tok.items()}}

    @classmethod
    def load(cls, data: dict, valid_intents: set[str] | None = None) -> "IntentModel":
        m = cls()
        if not isinstance(data, dict):
            return m
        allowed = set(SEEDS) if valid_intents is None else set(valid_intents)
        raw_docs = data.get("docs") if isinstance(data.get("docs"), dict) else {}
        raw_tok = data.get("tok") if isinstance(data.get("tok"), dict) else {}
        for intent, val in raw_docs.items():
            try:
                num = float(val)
            except (TypeError, ValueError):
                num = -1
            if intent in allowed and num > 0:
                m.docs[intent] = num
            else:
                m.filtered_junk = True
        for intent, counts in raw_tok.items():
            if intent not in allowed or intent not in m.docs or not isinstance(counts, dict):
                if intent not in allowed:
                    m.filtered_junk = True
                continue
            cleaned = Counter()
            for tok, cnt in counts.items():
                try:
                    w = float(cnt)
                except (TypeError, ValueError):
                    continue
                if isinstance(tok, str) and tok and " " not in tok and w > 0:
                    cleaned[tok] = w
            if cleaned:
                m.tok[intent] = cleaned
                m.vocab.update(cleaned)
            else:
                m.docs.pop(intent, None)
        return m


def L(mid, cold=None, warm=None):
    return {"mid": mid, "cold": cold or mid, "warm": warm or mid}


_FALLBACK_PERSONA = {
    "PH": {
        "first_meet": {"ru": L(["Новый контакт. Я — Serial Designation J. Назови имя."]),
                       "en": L(["New contact. I'm Serial Designation J. State your name."])},
        "greet": {"ru": L(["Здравствуй, {name}. По делу."]), "en": L(["Hello, {name}. State your business."])},
        "unknown": {"ru": L(["Не поняла запрос. Научи меня: «учись: фраза => ответ»."]),
                    "en": L(["Didn't get that. Teach me: \"learn: phrase => reply\"."])},
        "spam": {"ru": L(["Прекрати флуд, {name}."]), "en": L(["Stop flooding, {name}."])},
        "embarrass": {"ru": L(["П-погоди, {name}?! Я на смене!"], ["Прекрати нести чушь, {name}!"],
                              ["Кхм!.. {name}, не при N же..."])},
    },
    "ORDER_IDEAS": {"ru": ["проверить KPI"], "en": ["audit KPIs"]},
    "HELP": {"ru": "Команды: анекдот · сгенерируй: тема · время · статистика · учись: фраза => ответ · пока",
             "en": "Commands: joke · generate: prompt · time · stats · learn: phrase => reply · bye"},
    "QUIZ": [{"id": "b0", "ru": "Язык этого кода?", "en": "Language of this code?", "a": ["python", "питон"]}],
    "SYSTEM_PROMPT": {"ru": "Ты — Serial Designation J (Murder Drones).", "en": "You are Serial Designation J."},
}


def _load_seed_persona() -> dict:
    try:
        loaded = json.loads(PERSONA_FILE.read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and isinstance(loaded.get("PH"), dict):
            return loaded
    except (OSError, ValueError):
        pass
    return copy.deepcopy(_FALLBACK_PERSONA)


DEFAULT_PERSONA = _load_seed_persona()
_DEFAULT_PH = DEFAULT_PERSONA["PH"]
_DEFAULT_ORDER_IDEAS = DEFAULT_PERSONA.get("ORDER_IDEAS", _FALLBACK_PERSONA["ORDER_IDEAS"])
_DEFAULT_HELP = DEFAULT_PERSONA.get("HELP", _FALLBACK_PERSONA["HELP"])
_DEFAULT_QUIZ = DEFAULT_PERSONA.get("QUIZ", _FALLBACK_PERSONA["QUIZ"])
_DEFAULT_SYSTEM_PROMPT = DEFAULT_PERSONA.get("SYSTEM_PROMPT", _FALLBACK_PERSONA["SYSTEM_PROMPT"])
PH = copy.deepcopy(_DEFAULT_PH)
ORDER_IDEAS = copy.deepcopy(_DEFAULT_ORDER_IDEAS)
HELP = copy.deepcopy(_DEFAULT_HELP)
QUIZ = copy.deepcopy(_DEFAULT_QUIZ)
SYSTEM_PROMPT = copy.deepcopy(_DEFAULT_SYSTEM_PROMPT)


def _sync_persona_globals(persona: dict) -> None:
    global SYSTEM_PROMPT
    PH.clear()
    PH.update(copy.deepcopy(persona.get("PH", _DEFAULT_PH)))
    ORDER_IDEAS.clear()
    ORDER_IDEAS.update(copy.deepcopy(persona.get("ORDER_IDEAS", _DEFAULT_ORDER_IDEAS)))
    HELP.clear()
    HELP.update(copy.deepcopy(persona.get("HELP", _DEFAULT_HELP)))
    QUIZ[:] = copy.deepcopy(persona.get("QUIZ", _DEFAULT_QUIZ))
    SYSTEM_PROMPT = copy.deepcopy(persona.get("SYSTEM_PROMPT", _DEFAULT_SYSTEM_PROMPT))


def _merge_persona(default: dict, loaded: dict) -> tuple[dict, bool]:
    merged, changed = copy.deepcopy(loaded), False
    if not isinstance(merged.get("PH"), dict):
        merged["PH"], changed = copy.deepcopy(default["PH"]), True
    else:
        for key, langs in default["PH"].items():
            ph_k = merged["PH"].setdefault(key, None)
            if not isinstance(ph_k, dict):
                merged["PH"][key], changed = copy.deepcopy(langs), True
                continue
            for lang, tones in langs.items():
                ph_l = ph_k.setdefault(lang, None)
                if not isinstance(ph_l, dict):
                    ph_k[lang], changed = copy.deepcopy(tones), True
                    continue
                for tone, lines in tones.items():
                    if not isinstance(ph_l.get(tone), list) or not ph_l[tone]:
                        ph_l[tone], changed = copy.deepcopy(lines), True
    for sec in ("ORDER_IDEAS", "HELP"):
        if not isinstance(merged.get(sec), dict):
            merged[sec], changed = copy.deepcopy(default[sec]), True
        else:
            for lang, val in default[sec].items():
                if not isinstance(merged[sec].get(lang), type(val)) or not merged[sec][lang]:
                    merged[sec][lang], changed = copy.deepcopy(val), True
    if not isinstance(merged.get("QUIZ"), list) or not merged["QUIZ"]:
        merged["QUIZ"], changed = copy.deepcopy(default["QUIZ"]), True
    sp = merged.get("SYSTEM_PROMPT")
    if isinstance(sp, dict):
        for lang, val in default["SYSTEM_PROMPT"].items():
            if not isinstance(sp.get(lang), str) or not sp[lang].strip():
                sp[lang], changed = val, True
    elif not (isinstance(sp, str) and sp.strip()):
        merged["SYSTEM_PROMPT"], changed = copy.deepcopy(default["SYSTEM_PROMPT"]), True
    return merged, changed


def load_persona(path: Path | None = None) -> tuple[dict, str]:
    global PERSONA_ERROR
    persona = copy.deepcopy(DEFAULT_PERSONA)
    target = Path(path) if path else data_path("j_persona.json", "J_PERSONA")
    try:
        loaded = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _atomic_write_json(target, persona, backup=False, indent=1)
        PERSONA_ERROR = ""
        _sync_persona_globals(persona)
        return persona, ""
    except json.JSONDecodeError as exc:
        PERSONA_ERROR = f"ошибка в j_persona.json: строка {exc.lineno}, столбец {exc.colno} ({exc.msg}). Использую стандартный характер."
        _sync_persona_globals(persona)
        return persona, PERSONA_ERROR
    except OSError as exc:
        PERSONA_ERROR = f"не удалось прочитать j_persona.json: {exc}"
        _sync_persona_globals(persona)
        return persona, PERSONA_ERROR
    if not isinstance(loaded, dict):
        PERSONA_ERROR = "ошибка в j_persona.json: корень файла должен быть JSON-объектом. Использую стандартный характер."
        _sync_persona_globals(persona)
        return persona, PERSONA_ERROR
    merged, changed = _merge_persona(DEFAULT_PERSONA, loaded)
    if changed:
        _atomic_write_json(target, merged, backup=False, indent=1)
    PERSONA_ERROR = ""
    _sync_persona_globals(merged)
    return merged, ""


LOCAL_JOKES = {
    "ru": [
        ("corporate", "Сколько сотрудников нужно, чтобы заменить лампочку?", "Одного. Остальные уволены за вопросы."),
        ("n_roast", "Знаешь, чем N полезен для отряда?", "Рядом с ним я всегда выгляжу компетентной."),
        ("programming", "Баг — это незапланированная функция.", "Как и ты в моём расписании."),
        ("dirty", "Ты просишь пошлую шутку в рабочее время?", "Хорошо: много суеты и ноль результата по KPI."),
        ("roast", "Я пыталась найти повод тебя похвалить,", "но аудит вернул ошибку 404."),
    ],
    "en": [
        ("corporate", "How many employees to change a bulb?", "One. The rest were fired for asking."),
        ("n_roast", "What's N good for in this squad?", "Next to him I always look competent."),
        ("programming", "A bug is an unplanned feature.", "Like you in my schedule."),
        ("dirty", "Asking for a dirty joke on company hours?", "Lots of noise, zero KPI delivery."),
        ("roast", "I tried to find one reason to praise you,", "and got 404: competence not found."),
    ],
}
MOVES = ("rock", "paper", "scissors")
BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
COUNTER = {"rock": "paper", "paper": "scissors", "scissors": "rock"}
MOVE_NAMES = {"ru": {"rock": "камень", "paper": "бумага", "scissors": "ножницы"},
              "en": {"rock": "rock", "paper": "paper", "scissors": "scissors"}}
RPS_PARSE = {
    "камень": "rock", "камня": "rock", "к": "rock", "rock": "rock", "r": "rock", "stone": "rock",
    "бумага": "paper", "бумагу": "paper", "б": "paper", "paper": "paper", "p": "paper",
    "ножницы": "scissors", "н": "scissors", "scissors": "scissors", "s": "scissors",
}


def load_jokes(path: Path | None = None) -> dict[str, list[dict]]:
    out = {"ru": [{"id": f"L{i}", "cat": c, "setup": s, "punch": p} for i, (c, s, p) in enumerate(LOCAL_JOKES["ru"])],
           "en": [{"id": f"L{i}", "cat": c, "setup": s, "punch": p} for i, (c, s, p) in enumerate(LOCAL_JOKES["en"])]}
    if path is None:
        cand = data_dir() / "jokes.json"
        path = cand if cand.is_file() else JOKES_FILE
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        content = item.get("content") if isinstance(item.get("content"), dict) else {}
        for lang in out:
            body = content.get(lang) or item.get(lang)
            if isinstance(body, dict) and body.get("setup"):
                out[lang].append({"id": f"F{item.get('id', len(out[lang]))}", "cat": item.get("category", "misc"),
                                  "setup": body["setup"], "punch": body.get("punch") or body.get("punchline", "")})
    return out


def new_profile() -> dict:
    return {
        "name": None, "lang": None, "respect": 0, "dev": False, "patience": 100.0, "seen": 0, "last_ts": time.time(),
        "taught": [], "custom_q": [], "places": [], "facts": {},
        "stats": {"rps": {"w": 0, "l": 0, "d": 0, "rounds": 0, "hits": 0}, "guess": {"w": 0, "l": 0}, "quiz": {"right": 0, "wrong": 0}},
        "rps_model": {"o1": {}, "o2": {}}, "rps_last": [], "guess_n": 20, "quiz_w": {}, "joke_w": {}, "joke_seen": [],
    }


def _upgrade_profile(p: dict) -> dict:
    for k, v in new_profile().items():
        if k not in p:
            p[k] = copy.deepcopy(v)
    if not isinstance(p.get("facts"), dict):
        p["facts"] = {}
    return p


def _merge_list_unique(dst: list, src: list, key_fn, prep_fn=lambda x, _: copy.deepcopy(x)) -> None:
    seen = {key_fn(x) for x in dst if key_fn(x)}
    for item in src:
        k = key_fn(item)
        if k and k not in seen:
            seen.add(k)
            dst.append(prep_fn(item, len(dst)))


def _merge_profiles(dst: dict, src: dict) -> dict:
    _upgrade_profile(dst)
    _upgrade_profile(src)
    dst["name"] = dst.get("name") or src.get("name")
    dst["lang"] = dst.get("lang") or src.get("lang")
    resp = max(float(dst.get("respect", 0)), float(src.get("respect", 0)))
    dst["respect"] = int(resp) if resp.is_integer() else resp
    dst["dev"] = bool(dst.get("dev") or src.get("dev"))
    dst["patience"] = max(float(dst.get("patience", 100.0)), float(src.get("patience", 100.0)))
    dst["seen"] = int(dst.get("seen", 0)) + int(src.get("seen", 0))
    dst["last_ts"] = max(float(dst.get("last_ts", 0.0)), float(src.get("last_ts", 0.0)))
    if "respect_ts" in dst or "respect_ts" in src:
        dst["respect_ts"] = max(float(dst.get("respect_ts", 0.0)), float(src.get("respect_ts", 0.0)))
    _merge_list_unique(dst["taught"], [x for x in src.get("taught", []) if isinstance(x, dict) and x.get("a") is not None],
                       lambda x: norm(x["q"]) if isinstance(x, dict) and x.get("q") else "")
    _merge_list_unique(dst["custom_q"], [x for x in src.get("custom_q", []) if isinstance(x, dict)],
                       lambda x: norm(x["q"]) if isinstance(x, dict) and x.get("q") else "",
                       lambda x, n: {**copy.deepcopy(x), "id": f"c{n}"})
    if isinstance(dst.get("places"), list) and isinstance(src.get("places"), list):
        _merge_list_unique(dst["places"], src["places"], lambda x: norm(str(x)))
    for fk, fv in (src.get("facts") if isinstance(src.get("facts"), dict) else {}).items():
        if isinstance(fk, str) and fk.strip() and fv is not None and str(fv).strip():
            if not str(dst["facts"].get(fk) or "").strip():
                dst["facts"][fk] = str(fv).strip()
    for gk, keys in (("rps", ("w", "l", "d", "rounds", "hits")), ("guess", ("w", "l")), ("quiz", ("right", "wrong"))):
        for sk in keys:
            dst["stats"][gk][sk] = int(dst["stats"][gk].get(sk, 0)) + int(src["stats"].get(gk, {}).get(sk, 0))
    for ok in ("o1", "o2"):
        for sk, moves in (src["rps_model"].get(ok) or {}).items():
            if isinstance(moves, dict):
                tm = dst["rps_model"][ok].setdefault(sk, {})
                for mv, cnt in moves.items():
                    tm[mv] = int(tm.get(mv, 0)) + int(cnt)
    dst["rps_last"] = dst.get("rps_last") or list(src.get("rps_last", []))[-10:]
    dst["guess_n"] = max(int(dst.get("guess_n", 20)), int(src.get("guess_n", 20)))
    for wk in ("quiz_w", "joke_w"):
        for k, v in (src.get(wk) if isinstance(src.get(wk), dict) else {}).items():
            dst[wk].setdefault(k, v)
    dst["joke_seen"] = list(dict.fromkeys(list(dst.get("joke_seen", [])) + list(src.get("joke_seen", []))))[-20:]
    if isinstance(src.get("syn"), dict):
        dst.setdefault("syn", {}).update({k: v for k, v in src["syn"].items() if k not in dst.get("syn", {})})
    if "mc_last" not in dst and "mc_last" in src:
        dst["mc_last"] = copy.deepcopy(src["mc_last"])
    return dst


def tone_of(p: dict) -> str:
    if p.get("dev"):
        return "cold" if p["patience"] < 35 else "warm"
    if p["patience"] < 35 or p["respect"] < 15:
        return "cold"
    return "warm" if p["respect"] >= 60 else "mid"


class Session:
    __slots__ = ("state", "last_text", "last_joke", "history", "done")
    MAX_HISTORY = 8

    def __init__(self) -> None:
        self.state: dict | None = None
        self.last_text = ""
        self.last_joke: dict | None = None
        self.history: list[tuple[str, str]] = []
        self.done = False

    def remember_turn(self, user_text: str, reply: str) -> None:
        u, r = (user_text or "").strip(), (reply or "").strip()
        if u and r:
            self.history.append((u[:400], r[:500]))
            del self.history[:-self.MAX_HISTORY]


class SpamGuard:
    def __init__(self, window: float = 10.0, repeat_window: float = 1.5, max_repeats: int = 6) -> None:
        self.window, self.repeat_window, self.max_repeats = window, repeat_window, max_repeats
        self._history: dict[str, list[tuple[float, str]]] = defaultdict(list)

    @staticmethod
    def limit() -> int:
        try:
            raw = os.environ.get("J_SPAM_LIMIT", "15")
            return int("15" if raw is None or not str(raw).strip() else raw)
        except ValueError:
            return 15

    def check(self, uid: str, text: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        key, hist = norm(text), self._history[uid]
        hist[:] = [(t, k) for t, k in hist if t >= now - self.window]
        hist.append((now, key))
        lim = self.limit()
        del hist[:-max(lim + 10, 50)]
        if lim > 0 and len(hist) > lim:
            return True
        cutoff = now - self.repeat_window
        streak = 0
        for t, k in reversed(hist):
            if k == key and t >= cutoff:
                streak += 1
            else:
                break
        return streak > self.max_repeats


def is_offline_forced() -> bool:
    return (os.environ.get("J_OFFLINE_MODE", "0") or "0").strip().lower() in ("1", "true", "yes", "on", "да")


def is_local_or_lan_host(host: str) -> bool:
    """True для loopback (127.0.0.1/localhost/::1), локальной сети (RFC1918/link-local) и .local/.lan."""
    h = (host or "").strip().strip("[]").lower()
    if not h:
        return False
    if h in ("localhost", "127.0.0.1", "::1") or h.endswith(".local") or h.endswith(".lan"):
        return True
    try:
        ip = ipaddress.ip_address(h)
        return ip.is_loopback or ip.is_private or ip.is_link_local
    except ValueError:
        return False


def is_local_or_lan_url(url: str) -> bool:
    raw = (url or "").strip()
    if not raw:
        return False
    parsed = urllib.parse.urlparse(raw)
    return (parsed.scheme or "").lower() in ("http", "https") and is_local_or_lan_host(parsed.hostname or "")


_DEFAULT_PEER_AGENTS = "N,Serial Designation N,SD-N,н,дрон н"
_N_AGENT_NAMES = {
    "n", "н", "serial designation n", "sd-n", "sd n", "serial_designation_n",
    "дрон н", "дрон n", "агент н", "агент n",
}


def is_peer_agent(uid: str = "", name: str = "") -> bool:
    """Проверяет, относится ли отправитель к разрешённым дружественным ИИ-агентам (например, N)."""
    raw = os.environ.get("J_PEER_AGENTS")
    if raw is None:
        raw = _DEFAULT_PEER_AGENTS
    peers = {norm(x) for x in raw.split(",") if x.strip()}
    u = norm(str(uid or ""))
    u_bare = u.split(":", 1)[1] if ":" in u else u
    nm = norm(str(name or ""))
    if not (u or nm):
        return False
    if u in peers or u_bare in peers or nm in peers:
        return True
    if u.startswith("ag:") and u_bare in (_N_AGENT_NAMES | peers):
        return True
    return False


def is_agent_n(uid: str = "", name: str = "") -> bool:
    """Проверяет, является ли собеседник вторым ИИ-агентом отряда — Serial Designation N."""
    u = norm(str(uid or ""))
    u_bare = u.split(":", 1)[1] if ":" in u else u
    nm = norm(str(name or ""))
    if u_bare in _N_AGENT_NAMES or nm in _N_AGENT_NAMES:
        return True
    if is_peer_agent(uid, name):
        parts = set(re.split(r"[\s_\-:]+", f"{nm} {u_bare}".strip()))
        if "n" in parts or "н" in parts or not nm:
            return True
    return False


class PeerGuard:
    """Защита от бесконечных циклов диалога между двумя ИИ-агентами в общих каналах."""

    def __init__(self, reset_window: float = 120.0) -> None:
        self.reset_window = reset_window
        self._lock = threading.RLock()
        self._turns: dict[str, int] = {}
        self._last_ts: dict[str, float] = {}

    @staticmethod
    def _max_turns() -> int:
        try:
            return max(1, int(os.environ.get("J_PEER_MAX_TURNS", "3") or 3))
        except ValueError:
            return 3

    @staticmethod
    def _cooldown() -> float:
        try:
            return max(0.0, float(os.environ.get("J_PEER_COOLDOWN", "4") or 4))
        except ValueError:
            return 4.0

    def note_human(self, channel_id: str = "default") -> None:
        key = str(channel_id or "default")
        with self._lock:
            self._turns[key] = 0
            self._last_ts.pop(key, None)

    def allow_turn(
        self,
        peer_uid: str = "",
        channel_id: str = "default",
        check_cooldown: bool = True,
        now: float | None = None,
    ) -> tuple[bool, bool]:
        key = str(channel_id or peer_uid or "default")
        ts = time.time() if now is None else now
        max_turns = self._max_turns()
        cooldown = self._cooldown()
        with self._lock:
            last = self._last_ts.get(key, 0.0)
            count = self._turns.get(key, 0)
            if last > 0 and (ts - last) > self.reset_window:
                count = 0
            if check_cooldown and last > 0 and (ts - last) < cooldown:
                return False, False
            count += 1
            if count > max_turns:
                return False, False
            self._turns[key] = count
            self._last_ts[key] = ts
            return True, count == max_turns


class LLMClient:
    is_offline_forced = staticmethod(is_offline_forced)

    def __init__(self, opener=None, system_prompt_fn=None, timeout: float = 20.0) -> None:
        self.opener = opener or urllib.request.urlopen
        self._custom_opener = opener is not None
        self.system_prompt_fn = system_prompt_fn
        self.timeout = timeout
        self._fail_ts: dict[str, float] = {}

    @staticmethod
    def provider() -> str:
        raw = (os.environ.get("J_LLM_PROVIDER", "off") or "off").strip().lower()
        return raw if raw in ("off", "auto", "local", "ollama", "openai", "gemini") else "off"

    def enabled(self) -> bool:
        return self.provider() != "off"

    def _net_timeout(self) -> float:
        raw = os.environ.get("J_NET_TIMEOUT", "")
        if str(raw).strip():
            try:
                val = float(raw)
                if val > 0:
                    return val
            except (TypeError, ValueError):
                pass
        return float(self.timeout)

    def _retry_cooldown(self) -> float:
        if self._custom_opener:
            return 0.0
        raw = os.environ.get("J_NET_RETRY_SEC", "30")
        try:
            return max(0.0, float(raw if str(raw).strip() else 30))
        except (TypeError, ValueError):
            return 30.0

    def build_system_prompt(self, p: dict, base_prompt: str | None = None) -> str:
        lang = p.get("lang") or "ru"
        if base_prompt is None:
            sp = self.system_prompt_fn(p) if callable(self.system_prompt_fn) else SYSTEM_PROMPT
            base_prompt = (sp.get(lang) or sp.get("ru") or "") if isinstance(sp, dict) else str(sp or "")
        tone, name, resp, pat = tone_of(p), p.get("name") or "?", int(p.get("respect", 0)), max(0, int(p.get("patience", 100)))
        notes = ({"cold": "cold, irritable, abrupt", "mid": "strictly business, sharp corporate sarcasm", "warm": "dryly appreciative, loyal but still commanding"}
                 if lang == "en" else
                 {"cold": "холодный, раздражённый, резкий", "mid": "деловой, колкий, корпоративно-язвительный", "warm": "сдержанно-благосклонный, но всё равно командирский"})
        ctx = (f"Current state — tone: {tone} ({notes[tone]}); interlocutor: {name}; respect: {resp}%; patience: {pat}%. Reply in English."
               if lang == "en" else
               f"Текущее состояние — тон: {tone} ({notes[tone]}); собеседник: {name}; уважение: {resp}%; терпение: {pat}%. Отвечай на русском языке.")
        return f"{base_prompt.strip()}\n{ctx}".strip()

    def _read_resp(self, req: urllib.request.Request) -> dict:
        resp = self.opener(req, timeout=self._net_timeout())
        raw = resp.__enter__().read() if hasattr(resp, "__enter__") else resp.read()
        if hasattr(resp, "__exit__"):
            resp.__exit__(None, None, None)
        return json.loads(raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw))

    def _call_provider(self, prov: str, sys_prompt: str, prompt_text: str, turns: list[tuple[str, str]],
                       media_list: list[dict], url: str, model: str, key: str, auto_mode: bool = False) -> str | None:
        if prov == "ollama":
            use_url = url if (not auto_mode or is_local_or_lan_url(url)) else ""
            use_model = ("llama3.2" if (not model or model.lower().startswith(("gpt-", "o1", "o3", "o4", "gemini"))) else model) if auto_mode else (model or "llama3.2")
            ep = use_url.rstrip("/") if use_url.rstrip("/").endswith(("/api/chat", "/api/generate")) else (use_url.rstrip("/") or "http://127.0.0.1:11434") + "/api/chat"
            payload = ({"model": use_model, "system": sys_prompt, "prompt": "\n".join([f"User: {u}\nJ: {a}" for u, a in turns] + [prompt_text]), "stream": False}
                       if ep.endswith("/api/generate") else
                       {"model": use_model, "messages": [{"role": "system", "content": sys_prompt}, *[m for u, a in turns for m in ({"role": "user", "content": u}, {"role": "assistant", "content": a})], {"role": "user", "content": prompt_text}], "stream": False})
            data = self._read_resp(urllib.request.Request(ep, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json"}))
            return ((data.get("message") or {}).get("content") or data.get("response") or "").strip() or None
        if prov == "openai":
            use_model = model or "gpt-4o-mini"
            ep = (url.rstrip("/") if url.rstrip("/").endswith(("/chat/completions", "/completions", "/responses"))
                  else (url.rstrip("/") + "/chat/completions" if url.rstrip("/").endswith("/v1")
                        else (url.rstrip("/") + "/v1/chat/completions" if url else "https://api.openai.com/v1/chat/completions")))
            headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})}
            imgs = [{"type": "image_url", "image_url": {"url": f"data:{m['mime']};base64,{m['data_b64']}"}}
                    for m in media_list if m.get("kind") == "image" and m.get("data_b64") and m.get("mime")]
            user_msg = {"role": "user", "content": [{"type": "text", "text": prompt_text}, *imgs] if imgs else prompt_text}
            msgs = [{"role": "system", "content": sys_prompt}, *[m for u, a in turns for m in ({"role": "user", "content": u}, {"role": "assistant", "content": a})], user_msg]
            data = self._read_resp(urllib.request.Request(ep, data=json.dumps({"model": use_model, "messages": msgs, "temperature": 0.7, "max_tokens": 350}, ensure_ascii=False).encode("utf-8"), headers=headers))
            c = (data.get("choices") or [{}])[0]
            return str((c.get("message") or {}).get("content") or c.get("text") or "").strip() or None
        if prov == "gemini":
            use_model = model or "gemini-2.0-flash"
            ep = url or f"https://generativelanguage.googleapis.com/v1beta/models/{use_model}:generateContent"
            headers = {"Content-Type": "application/json", **({"x-goog-api-key": key} if key else {})}
            uparts = [{"text": prompt_text}, *({"inline_data": {"mime_type": m["mime"], "data": m["data_b64"]}} for m in media_list if m.get("data_b64") and m.get("mime"))]
            contents = [*[x for u, a in turns for x in ({"role": "user", "parts": [{"text": u}]}, {"role": "model", "parts": [{"text": a}]})], {"role": "user", "parts": uparts}]
            data = self._read_resp(urllib.request.Request(ep, data=json.dumps({"system_instruction": {"parts": [{"text": sys_prompt}]}, "contents": contents}, ensure_ascii=False).encode("utf-8"), headers=headers))
            parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
            return "".join(str(pt.get("text", "")) for pt in parts if isinstance(pt, dict)).strip() or None
        return None

    def generate(self, p: dict, text: str, system_prompt: str | None = None,
                 history: list[tuple[str, str]] | None = None, context: str = "",
                 media: list[dict] | None = None) -> str | None:
        prov = self.provider()
        if prov == "off" or (not (text or "").strip() and not media):
            return None
        offline = is_offline_forced()
        url, model, key = (os.environ.get("J_LLM_URL", "").strip(), os.environ.get("J_LLM_MODEL", "").strip(), os.environ.get("J_LLM_KEY", "").strip())
        local_url = is_local_or_lan_url(url)
        if offline:
            if prov in ("openai", "gemini") and not local_url:
                return None
            if prov in ("ollama", "local") and url and not local_url:
                return None
        if prov == "local":
            prov = "ollama"
        if prov == "auto":
            candidates = ["openai"] if (local_url and ("/v1" in url.lower() or "completions" in url.lower())) else ["ollama"]
            if not offline and key:
                cloud = "gemini" if (key.startswith("AIza") or model.lower().startswith("gemini")) else "openai"
                if cloud not in candidates:
                    candidates.append(cloud)
        else:
            candidates = [prov]
        retry_sec, now = self._retry_cooldown(), time.time()
        active = [c for c in candidates if not (retry_sec > 0 and self._fail_ts.get(c) is not None and (now - self._fail_ts[c]) < retry_sec)]
        if not active:
            return None
        sys_prompt = self.build_system_prompt(p, system_prompt)
        if context and context.strip():
            sys_prompt += f"\nКонтекст / Context: {context.strip()}"
        media_list = [m for m in (media or []) if isinstance(m, dict)]
        notes = [f"[{m.get('kind', 'file')}: {m.get('filename') or 'file'} ({m.get('mime') or 'application/octet-stream'})]"
                 + (f"\n{m['text_preview'][:1500]}" if m.get("text_preview") else "") for m in media_list]
        prompt_text = ((text or "").strip() or "Проанализируй приложенные медиафайлы.") + (("\n\n" + "\n".join(notes)) if notes else "")
        turns = list(history or [])[-8:]
        for cand in active:
            try:
                reply = self._call_provider(cand, sys_prompt, prompt_text, turns, media_list, url, model, key, auto_mode=(prov == "auto"))
                if reply:
                    self._fail_ts.pop(cand, None)
                    return reply
            except Exception:
                self._fail_ts[cand] = time.time()
        return None


_UNIT_SPEC = [
    ("time", 1.0, "секунда,секунды,секунд,секунде,секундах,секунду,сек,second,seconds,sec,secs", ("секунда", "секунды", "секунд"), ("second", "seconds")),
    ("time", 60.0, "минута,минуты,минут,минуте,минутах,минуту,мин,minute,minutes,min,mins", ("минута", "минуты", "минут"), ("minute", "minutes")),
    ("time", 3600.0, "час,часа,часов,часе,часах,ч,hour,hours,hr,hrs,h", ("час", "часа", "часов"), ("hour", "hours")),
    ("time", 86400.0, "сутки,суток,сутках,день,дня,дней,дне,днях,day,days", ("сутки", "суток", "суток"), ("day", "days")),
    ("time", 604800.0, "неделя,недели,недель,неделе,неделях,неделю,week,weeks", ("неделя", "недели", "недель"), ("week", "weeks")),
    ("len", 1e-6, "мкм,микрометр,микрометра,микрометров,микрометре,микрометрах,микрометры,um,micrometer,micrometers,micrometre,micrometres", ("микрометр", "микрометра", "микрометров"), ("micrometer", "micrometers")),
    ("len", 0.001, "миллиметр,миллиметра,миллиметров,миллиметре,миллиметрах,миллиметры,мм,millimeter,millimeters,millimetre,millimetres,mm", ("миллиметр", "миллиметра", "миллиметров"), ("millimeter", "millimeters")),
    ("len", 0.01, "сантиметр,сантиметра,сантиметров,сантиметре,сантиметрах,сантиметры,см,centimeter,centimeters,centimetre,centimetres,cm", ("сантиметр", "сантиметра", "сантиметров"), ("centimeter", "centimeters")),
    ("len", 0.0254, "дюйм,дюйма,дюймов,дюйме,дюймах,дюймы,in,inch,inches", ("дюйм", "дюйма", "дюймов"), ("inch", "inches")),
    ("len", 0.3048, "фут,фута,футов,футе,футах,футы,ft,foot,feet", ("фут", "фута", "футов"), ("foot", "feet")),
    ("len", 1.0, "метр,метра,метров,метре,метрах,метры,м,meter,meters,metre,metres,m", ("метр", "метра", "метров"), ("meter", "meters")),
    ("len", 1000.0, "километр,километра,километров,километре,километрах,километры,км,kilometer,kilometers,kilometre,kilometres,km", ("километр", "километра", "километров"), ("kilometer", "kilometers")),
    ("len", 1609.344, "миля,мили,миль,миле,милях,милю,mile,miles,mi", ("миля", "мили", "миль"), ("mile", "miles")),
    ("len", 1852.0, "морскаямиля,морскиемили,морскихмиль,морскоймиле,nmi", ("морская миля", "морские мили", "морских миль"), ("nautical mile", "nautical miles")),
    ("len", 149597870700.0, "ае,а.е.,au", ("а.е.", "а.е.", "а.е."), ("AU", "AU")),
    ("mass", 1.0, "грамм,грамма,граммов,грамме,граммах,граммы,г,гр,gram,grams,g", ("грамм", "грамма", "граммов"), ("gram", "grams")),
    ("mass", 1000.0, "килограмм,килограмма,килограммов,килограмме,килограммах,килограммы,кг,kilogram,kilograms,kg", ("килограмм", "килограмма", "килограммов"), ("kilogram", "kilograms")),
    ("mass", 1000000.0, "тонна,тонны,тонн,тонне,тоннах,тонну,т,ton,tons,tonne,tonnes", ("тонна", "тонны", "тонн"), ("ton", "tons")),
    ("speed", 1.0, "м/с,мс,m/s,mps", ("м/с", "м/с", "м/с"), ("m/s", "m/s")),
    ("speed", 1.0 / 3.6, "км/ч,кмч,km/h,kph,kmh", ("км/ч", "км/ч", "км/ч"), ("km/h", "km/h")),
    ("speed", 1000.0, "км/с,кмс,km/s,kms", ("км/с", "км/с", "км/с"), ("km/s", "km/s")),
    ("speed", 1852.0 / 3600.0, "узел,узла,узлов,узле,узлах,узлы,knot,knots,kt", ("узел", "узла", "узлов"), ("knot", "knots")),
    ("speed", 343.0, "мах,маха,махов,махе,махах,махи,mach", ("Мах", "Маха", "Махов"), ("Mach", "Mach")),
    ("pressure", 1.0, "па,паскаль,паскаля,паскалей,паскале,паскалях,паскали,pa,pascal,pascals", ("паскаль", "паскаля", "паскалей"), ("pascal", "pascals")),
    ("pressure", 1000.0, "кпа,килопаскаль,килопаскаля,килопаскалей,килопаскале,килопаскалях,килопаскали,kpa", ("кПа", "кПа", "кПа"), ("kPa", "kPa")),
    ("pressure", 1e6, "мпа,мегапаскаль,мегапаскаля,мегапаскалей,мегапаскале,мегапаскалях,мегапаскали,mpa", ("МПа", "МПа", "МПа"), ("MPa", "MPa")),
    ("pressure", 100000.0, "бар,бара,баров,баре,барах,бары,bar,bars", ("бар", "бара", "баров"), ("bar", "bars")),
    ("pressure", 101325.0, "атм,атмосфера,атмосферы,атмосфер,атмосфере,атмосферах,атмосферу,atm", ("атм", "атм", "атм"), ("atm", "atm")),
    ("pressure", 133.322368, "торр,торра,торров,торре,торрах,торры,ммртст,torr,mmhg", ("торр", "торр", "торр"), ("torr", "torr")),
    ("pressure", 6894.757, "psi", ("psi", "psi", "psi"), ("psi", "psi")),
    ("angle", 1.0, "градус,градуса,градусов,градусе,градусах,градусы,град,deg,degree,degrees", ("градус", "градуса", "градусов"), ("degree", "degrees")),
    ("angle", 180.0 / math.pi, "радиан,радиана,радианов,радиане,радианах,радианы,рад,rad,radian,radians", ("радиан", "радиана", "радианов"), ("radian", "radians")),
    ("force", 1.0, "ньютон,ньютона,ньютонов,ньютоне,ньютонах,ньютоны,н,n,newton,newtons", ("ньютон", "ньютона", "ньютонов"), ("newton", "newtons")),
    ("force", 1000.0, "килоньютон,килоньютона,килоньютонов,килоньютоне,килоньютонах,килоньютоны,кн,kn,kilonewton,kilonewtons", ("кН", "кН", "кН"), ("kN", "kN")),
    ("force", 1e6, "меганьютон,меганьютона,меганьютонов,меганьютоне,меганьютонах,меганьютоны,мн,mn,meganewton,meganewtons", ("МН", "МН", "МН"), ("MN", "MN")),
    ("force", 9.80665, "кгс,kgf", ("кгс", "кгс", "кгс"), ("kgf", "kgf")),
    ("power", 1.0, "ватт,ватта,ваттов,ватте,ваттах,ватты,вт,w,watt,watts", ("Вт", "Вт", "Вт"), ("watt", "watts")),
    ("power", 1000.0, "киловатт,киловатта,киловаттов,киловатте,киловаттах,киловатты,квт,kw,kilowatt,kilowatts", ("кВт", "кВт", "кВт"), ("kW", "kW")),
    ("power", 1e6, "мегаватт,мегаватта,мегаваттов,мегаватте,мегаваттах,мегаватты,мвт,mw,megawatt,megawatts", ("МВт", "МВт", "МВт"), ("MW", "MW")),
    ("energy", 1.0, "джоуль,джоуля,джоулей,джоуле,джоулях,джоули,дж,j,joule,joules", ("джоуль", "джоуля", "джоулей"), ("joule", "joules")),
    ("energy", 1000.0, "килоджоуль,килоджоуля,килоджоулей,килоджоуле,килоджоулях,килоджоули,кдж,kj,kilojoule,kilojoules", ("кДж", "кДж", "кДж"), ("kJ", "kJ")),
    ("energy", 1e6, "мегаджоуль,мегаджоуля,мегаджоулей,мегаджоуле,мегаджоулях,мегаджоули,мдж,mj,megajoule,megajoules", ("МДж", "МДж", "МДж"), ("MJ", "MJ")),
    ("energy", 3600.0, "ваттчас,ваттчаса,ваттчасов,ваттчасы,ватт-час,ватт-часа,ватт-часов,ватт-часы,втч,вт-ч,wh", ("Вт·ч", "Вт·ч", "Вт·ч"), ("Wh", "Wh")),
    ("energy", 3600000.0, "киловаттчас,киловаттчаса,киловаттчасов,киловаттчасы,киловатт-час,киловатт-часа,киловатт-часов,киловатт-часы,квтч,квт-ч,kwh", ("кВт·ч", "кВт·ч", "кВт·ч"), ("kWh", "kWh")),
]

_TEMP_UNITS: dict[str, str] = {
    **{w: "C" for w in ("цельсий", "цельсия", "цельсии", "цельсию", "цельсиях", "c", "°c", "с", "°с", "celsius")},
    **{w: "K" for w in ("кельвин", "кельвина", "кельвинов", "кельвину", "кельвине", "кельвинах", "кельвины", "k", "к", "°k", "°к", "kelvin", "kelvins")},
    **{w: "F" for w in ("фаренгейт", "фаренгейта", "фаренгейту", "фаренгейте", "фаренгейтах", "фаренгейты", "f", "°f", "ф", "°ф", "fahrenheit")},
}
_TEMP_LABELS: dict[str, str] = {"C": "°C", "K": "K", "F": "°F"}


class OpenResponder:
    _OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
            ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
    _UOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
    _MATH_FUNCS = {
        "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan,
        "asin": math.asin, "acos": math.acos, "atan": math.atan,
        "log": math.log, "ln": math.log, "log10": math.log10,
        "exp": math.exp, "abs": abs, "deg": math.degrees, "rad": math.radians,
    }
    _MATH_CONSTS = {"pi": math.pi, "e": math.e, "g0": 9.80665, "c": 299792458.0}
    _UNITS = {w: (cat, mul) for cat, mul, words, _, _ in _UNIT_SPEC for w in words.split(",")}
    _UNIT_LABELS = {(cat, mul): {"ru": ru, "en": en} for cat, mul, _, ru, en in _UNIT_SPEC}
    _BODIES = {
        "earth": ("Земли", "Earth", 398600.4418, 6371.0),
        "moon": ("Луны", "Moon", 4902.8, 1737.4),
        "mars": ("Марса", "Mars", 42828.37, 3389.5),
    }

    _R_PERCENT = re.compile(r"^\s*(?:(?:сколько(?:\s+будет)?|посчитай|вычисли|найди|what(?:'s|\s+is)|calculate|compute)\s+)?(-?\d+(?:[.,]\d+)?)\s*(?:%|процент(?:а|ов)?|percent)\s+(?:от|of)\s+(-?\d+(?:[.,]\d+)?)\s*\??\s*$", re.I)
    _R_CONV_HOW = re.compile(r"^\s*(?:сколько(?:\s+будет)?|how\s+many)\s+([a-zа-яё°/.\-]+)\s+(?:(?:есть|будет|содержится|are(?:\s+there)?)\s+)?(?:в|во|in)\s+(?:(-?\d+(?:[.,]\d+)?)\s*|(?:a|an|одном|одной|одних)\s+)?([a-zа-яё°/.\-]+)\s*[?.!]?\s*$", re.I)
    _R_CONV_TO = re.compile(r"^\s*(?:(?:переведи|конвертируй|сколько\s+будет|what(?:'s|\s+is)|convert)\s+)?(-?\d+(?:[.,]\d+)?)\s*([a-zа-яё°/.\-]+)\s+(?:в|во|to|in)\s+([a-zа-яё°/.\-]+)\s*[?.!]?\s*$", re.I)
    _R_FOLLOWUP = re.compile(r"^\s*(?:(?:а|ну|и)\s+)?(?:почему(?:\s+так|\s+же)?|зачем(?:\s+так)?|как\s+(?:именно|так|это)|что\s+дальше|дальше|подробнее|расскажи\s+подробнее|обоснуй|поясни|аргументируй|(?:а\s+)?(?:ты\s+)?(?:как|что)\s+(?:ты\s+)?думаешь|why(?:\s+is\s+that|\s+so|\s+though)?|how\s+so|what\s+(?:next|else|about\s+it)|tell\s+me\s+more|elaborate|go\s+on)\s*\??\s*$", re.I)
    _R_MATH_PREFIX = re.compile(r"^(?:сколько\s+будет|посчитай|вычисли|реши|calculate|compute|what\s+is)\s+(.+?)\??$", re.I)
    _R_MATH_EXPR = re.compile(r"^[0-9a-zA-Z\s.+\-*/()%^,]+$")
    _R_DATE = re.compile(r"(?:какое\s+сегодня\s+число|какая\s+(?:сегодня\s+)?дата|какой\s+(?:сегодня\s+)?день(?:\s+недели)?|какой\s+сейчас\s+(?:год|месяц)|what\s+(?:date|day)\s+is\s+it|today'?s\s+date)", re.I)
    _R_COIN = re.compile(r"(?:подбрось\s+монет\w*|ор[её]л\s+или\s+решк\w*|flip\s+a?\s*coin|heads\s+or\s+tails)", re.I)
    _R_RAND = re.compile(r"(?:случайное\s+число|рандом\w*|random\s+number)\s*(?:от|from)?\s*(-?\d+)\s*(?:до|to|-)\s*(-?\d+)", re.I)
    _R_CHOICE = re.compile(r"^(?:(?:что\s+(?:выбрать|лучше)|выбери|посоветуй|choose(?:\s+between)?|pick)\s*[:\-]?\s*)?([^?]+?)\s+(?:или|or)\s+([^?]+?)\??$", re.I)
    _R_NON_CHOICE_HEAD = re.compile(r"^(?:почему|зачем|как\s+(?:работает|устроен)|что\s+такое|объясни|расскажи|why|how\s+(?:does|do|to)|what\s+(?:is|are)|explain)\b", re.I)
    _R_QUESTION_CUE = re.compile(r"(?:\?\s*$|^\s*(?:почему|зачем|как|что\s+(?:такое|думаешь|лучше|будет|если|значит)|кто|когда|где|куда|откуда|какой|какая|какое|какие|каков[аоы]?|чему\s+равн[аоы]?|сколько|стоит\s+ли|можно\s+ли|нужно\s+ли|правда\s+ли|объясни|подскажи|расскажи\s+про|why|how|what(?:'s|\s+is|\s+are|\s+do|\s+about)?|who|when|where|which|should|can|could|would|explain)\b)", re.I)
    _STOP_WORDS = set("почему зачем как что такое кто когда где куда откуда какой какая какое какие это или для про обо мне меня тебя тебе ты вы мы он она они то тут там уже еще ещё если есть быть надо можно нужно стоит ли очень просто вообще думаешь скажи подскажи объясни реально ладно хорошо давай кстати короче ну типа вот так блин интересно интерес ответь ответишь скажешь посмотрим дальше именно подробнее в на и а но же бы по за из от до why how what is are who when where which should can could would do does the a an to for of in on about okay ok well just you your me my we they it this that so".split())
    _DAYS_RU = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
    _MONTHS_RU = ("", "января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")

    @classmethod
    def _eval_math(cls, expr: str) -> float | int | None:
        cleaned = (expr or "").strip().lower().replace("^", "**").replace(",", ".")
        if not cleaned or len(cleaned) > 96 or "__" in cleaned or not cls._R_MATH_EXPR.match(cleaned):
            return None
        if not any(o in cleaned for o in "+-*/%") and "(" not in cleaned:
            return None
        def _walk(n):
            if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
                return n.value
            if isinstance(n, ast.Name) and n.id in cls._MATH_CONSTS:
                return cls._MATH_CONSTS[n.id]
            if isinstance(n, ast.UnaryOp) and type(n.op) in cls._UOPS:
                return cls._UOPS[type(n.op)](_walk(n.operand))
            if isinstance(n, ast.BinOp) and type(n.op) in cls._OPS:
                l, r = _walk(n.left), _walk(n.right)
                if (isinstance(n.op, ast.Pow) and abs(r) > 100) or (isinstance(n.op, (ast.Div, ast.FloorDiv, ast.Mod)) and r == 0):
                    raise ValueError
                return cls._OPS[type(n.op)](l, r)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in cls._MATH_FUNCS and len(n.args) == 1 and not n.keywords:
                return cls._MATH_FUNCS[n.func.id](_walk(n.args[0]))
            raise ValueError
        try:
            res = _walk(ast.parse(cleaned, mode="eval").body)
        except Exception:
            return None
        if not isinstance(res, (int, float)) or isinstance(res, bool):
            return None
        if isinstance(res, float):
            if math.isnan(res) or math.isinf(res):
                return None
            return 0 if res == 0.0 else (int(res) if res.is_integer() else round(res, 6))
        return res

    @staticmethod
    def _ru_plural(val: float | int, forms: tuple[str, ...]) -> str:
        if isinstance(val, float) and not val.is_integer():
            return forms[1]
        n = abs(int(val)) % 100
        return forms[2] if 11 <= n <= 19 else (forms[0] if n % 10 == 1 else (forms[1] if 2 <= n % 10 <= 4 else forms[2]))

    @staticmethod
    def _lookup_unit(raw: str, table: dict):
        k = norm(raw)
        return table.get(k) or table.get(k.rstrip(".,!?"))

    @classmethod
    def _convert_units(cls, text: str, lang: str) -> str | None:
        m = cls._R_CONV_HOW.match(text)
        if m:
            dst_raw, num_raw, src_raw = m.group(1), m.group(2), m.group(3)
        else:
            m2 = cls._R_CONV_TO.match(text)
            if not m2:
                return None
            num_raw, src_raw, dst_raw = m2.group(1), m2.group(2), m2.group(3)
        amount = float(num_raw.replace(",", ".")) if num_raw else 1.0
        src_t, dst_t = cls._lookup_unit(src_raw, _TEMP_UNITS), cls._lookup_unit(dst_raw, _TEMP_UNITS)
        if src_t and dst_t:
            c_val = amount if src_t == "C" else (amount - 273.15 if src_t == "K" else (amount - 32.0) * 5.0 / 9.0)
            t_val = c_val if dst_t == "C" else (c_val + 273.15 if dst_t == "K" else c_val * 9.0 / 5.0 + 32.0)
            val = round(t_val, 6)
            val = 0 if val == 0.0 else (int(val) if val.is_integer() else val)
            lbl = _TEMP_LABELS[dst_t]
            return f"Result: {val} {lbl}. Basic arithmetic." if lang == "en" else f"Ответ: {val} {lbl}. Элементарный расчёт."
        s_u, d_u = cls._lookup_unit(src_raw, cls._UNITS), cls._lookup_unit(dst_raw, cls._UNITS)
        if not s_u or not d_u or s_u[0] != d_u[0]:
            return None
        raw_val = amount * s_u[1] / d_u[1]
        val = round(raw_val, 6) if (raw_val == 0 or abs(raw_val) >= 1e-5) else float(f"{raw_val:.4g}")
        val = 0 if val == 0 else (int(val) if isinstance(val, float) and val.is_integer() else val)
        lbls = cls._UNIT_LABELS[(d_u[0], d_u[1])]
        if lang == "en":
            return f"Result: {val} {lbls['en'][0 if val == 1 else 1]}. Basic arithmetic."
        return f"Ответ: {val} {cls._ru_plural(val, lbls['ru'])}. Элементарный расчёт."

    @staticmethod
    def _calc_tsiolkovsky(low: str, lang: str) -> str | None:
        if not re.search(r"(?:дельта[\s\-]*[vв]|delta[\s\-]*[v]|циолковск\w*|tsiolkovsky)", low):
            return None
        m_m0 = re.search(r"\b(?:m_?0|м_?0|начальн\w*\s+масс\w*|initial\s+mass)\s*[:=]?\s*(-?\d+(?:[.,]\d+)?)\s*(т|тонн\w*|tons?|кг|kg)?", low)
        m_mf = re.search(r"\b(?:m_?[f1]|м_?[ф1]|конечн\w*\s+масс\w*|сух\w*\s+масс\w*|final\s+mass|dry\s+mass)\s*[:=]?\s*(-?\d+(?:[.,]\d+)?)\s*(т|тонн\w*|tons?|кг|kg)?", low)
        m_isp = re.search(r"\b(?:isp|уд(?:ельн\w*)?\.?\s*импульс\w*|specific\s+impulse)\s*[:=]?\s*(-?\d+(?:[.,]\d+)?)", low)
        m_ve = re.search(r"\b(?:ve|скорость\s+истечени\w*|exhaust\s+velocity)\s*[:=]?\s*(-?\d+(?:[.,]\d+)?)\s*(км/с|km/s|м/с|m/s)?", low)
        if not (m_m0 and m_mf and (m_isp or m_ve)):
            return None
        m0 = float(m_m0.group(1).replace(",", ".")) * (1000.0 if m_m0.group(2) and m_m0.group(2).startswith(("т", "ton")) else 1.0)
        mf = float(m_mf.group(1).replace(",", ".")) * (1000.0 if m_mf.group(2) and m_mf.group(2).startswith(("т", "ton")) else 1.0)
        if m0 <= 0 or mf <= 0 or m0 <= mf:
            return f"Ошибка расчёта Δv: требуется m0 > mf > 0 (сейчас m0={m0:g}, mf={mf:g})." if lang != "en" else f"Δv calculation error: requires m0 > mf > 0 (got m0={m0:g}, mf={mf:g})."
        if m_ve:
            ve = float(m_ve.group(1).replace(",", ".")) * (1000.0 if m_ve.group(2) in ("км/с", "km/s") else 1.0)
            if ve <= 0:
                return "Ошибка расчёта Δv: Ve должна быть > 0." if lang != "en" else "Δv calculation error: Ve must be > 0."
        else:
            isp = float(m_isp.group(1).replace(",", "."))
            if isp <= 0:
                return "Ошибка расчёта Δv: Isp должен быть > 0." if lang != "en" else "Δv calculation error: Isp must be > 0."
            ve = isp * 9.80665
        mr, prop_frac = m0 / mf, (m0 - mf) / m0
        dv = ve * math.log(mr)
        return (
            f"Δv = {dv:.2f} m/s ({dv / 1000.0:.3f} km/s); mass ratio m0/mf = {mr:.3f}; propellant mass fraction = {prop_frac * 100:.1f}% ({prop_frac:.3f})."
            if lang == "en"
            else f"Δv = {dv:.2f} м/с ({dv / 1000.0:.3f} км/с); отношение масс m0/mf = {mr:.3f}; доля топлива = {prop_frac * 100:.1f}% ({prop_frac:.3f})."
        )

    @classmethod
    def _calc_orbital(cls, low: str, lang: str) -> str | None:
        is_v1 = bool(re.search(r"(?:перв\w*\s+космическ\w*(?:\s+скорост\w*)?|first\s+cosmic\s+velocit\w*)", low))
        is_v2 = bool(re.search(r"(?:втор\w*\s+космическ\w*(?:\s+скорост\w*)?|escape\s+velocit\w*)", low))
        is_orb = bool(re.search(r"(?:орбитальн\w*\s+скорост\w*|период\w*\s+орбит\w*|кругов\w*\s+орбит\w*|orbital\s+velocit\w*|orbital\s+period)", low))
        if not (is_v1 or is_v2 or is_orb):
            return None
        b_key = "moon" if re.search(r"(?:лун[аеуы]?|moon)", low) else ("mars" if re.search(r"(?:марс[аеу]?|mars)", low) else "earth")
        b_ru, b_en, mu, r_km = cls._BODIES[b_key]
        m_alt = re.search(r"(?:высот[аеуы]?\s*(?:орбиты)?|alt(?:itude)?|\bh\b)\s*[:=]?\s*(-?\d+(?:[.,]\d+)?)\s*(км|km|м|m)?\b|(-?\d+(?:[.,]\d+)?)\s*(км|km)\b", low)
        h = None
        if m_alt:
            val_s, unit_s = m_alt.group(1) or m_alt.group(3), (m_alt.group(2) or m_alt.group(4) or "km").lower()
            h = float(val_s.replace(",", ".")) / (1000.0 if unit_s in ("м", "m") else 1.0)
            if h < 0:
                return "Ошибка орбитального расчёта: высота h должна быть >= 0 км." if lang != "en" else "Orbital calculation error: altitude h must be >= 0 km."
        if is_orb and not (is_v1 or is_v2) and h is None:
            return None
        alt = h if h is not None else 0.0
        r_orb = r_km + alt
        v = math.sqrt(mu / r_orb)
        v_esc = math.sqrt(2.0) * v
        period_min = (2.0 * math.pi * math.sqrt((r_orb ** 3) / mu)) / 60.0
        if h is not None:
            return (
                f"{b_en} circular orbit (h = {alt:g} km, R = {r_km:g} km): orbital velocity v = {v:.3f} km/s ({v * 1000.0:.1f} m/s), escape velocity = {v_esc:.3f} km/s, orbital period T = {period_min:.2f} min."
                if lang == "en"
                else f"Круговая орбита {b_ru} (h = {alt:g} км, R = {r_km:g} км): орбитальная скорость v = {v:.3f} км/с ({v * 1000.0:.1f} м/с), скорость убегания v2 = {v_esc:.3f} км/с, период обращения T = {period_min:.2f} мин."
            )
        if is_v1 and not is_v2:
            return f"First cosmic velocity for {b_en} (R = {r_km:g} km): v1 = {v:.3f} km/s ({v * 1000.0:.1f} m/s)." if lang == "en" else f"Первая космическая скорость {b_ru} (R = {r_km:g} км): v1 = {v:.3f} км/с ({v * 1000.0:.1f} м/с)."
        if is_v2 and not is_v1:
            return f"Escape velocity for {b_en} (R = {r_km:g} km): v2 = {v_esc:.3f} km/s ({v_esc * 1000.0:.1f} m/s)." if lang == "en" else f"Вторая космическая скорость (скорость убегания) {b_ru} (R = {r_km:g} км): v2 = {v_esc:.3f} км/с ({v_esc * 1000.0:.1f} м/с)."
        return f"Cosmic velocities for {b_en} (R = {r_km:g} km): v1 = {v:.3f} km/s ({v * 1000.0:.1f} m/s), v2 = {v_esc:.3f} km/s ({v_esc * 1000.0:.1f} m/s)." if lang == "en" else f"Космические скорости {b_ru} (R = {r_km:g} км): v1 = {v:.3f} км/с ({v * 1000.0:.1f} м/с), v2 = {v_esc:.3f} км/с ({v_esc * 1000.0:.1f} м/с)."

    @staticmethod
    def _calc_battery(low: str, lang: str) -> str | None:
        if not re.search(r"(?:автономност\w*|время\s+работы|запас\s+хода|батаре\w*|аккумулятор\w*|акб|battery|runtime|endurance)", low):
            return None
        m_wh = re.search(r"(-?\d+(?:[.,]\d+)?)\s*(квт[\-·*]?ч|квтч|kwh|ватт[\-·*]?час\w*|вт[\-·*]?ч|втч|wh)(?=$|[\s?,!;:.])", low)
        m_w = re.search(r"(-?\d+(?:[.,]\d+)?)\s*(квт|kw|ватт\w*|вт|w)(?=$|[\s?,!;:.])", low)
        m_ah = re.search(r"(-?\d+(?:[.,]\d+)?)\s*(ма[\-·*]?ч|мач|mah|а[\-·*]?ч|ач|ah)(?=$|[\s?,!;:.])", low)
        m_a = re.search(r"(-?\d+(?:[.,]\d+)?)\s*(ма|ma|ампер\w*|а|a)(?=$|[\s?,!;:.])", low)
        nom_h = None
        if m_wh and m_w:
            cap = float(m_wh.group(1).replace(",", ".")) * (1000.0 if m_wh.group(2).startswith(("квт", "kw")) else 1.0)
            load = float(m_w.group(1).replace(",", ".")) * (1000.0 if m_w.group(2) in ("квт", "kw") else 1.0)
            if cap <= 0 or load <= 0:
                return "Ошибка расчёта батареи: ёмкость и нагрузка должны быть > 0." if lang != "en" else "Battery calculation error: capacity and load must be > 0."
            nom_h = cap / load
        elif m_ah and m_a:
            cap = float(m_ah.group(1).replace(",", ".")) * (0.001 if m_ah.group(2).startswith(("ма", "ma")) else 1.0)
            load = float(m_a.group(1).replace(",", ".")) * (0.001 if m_a.group(2) in ("ма", "ma") else 1.0)
            if cap <= 0 or load <= 0:
                return "Ошибка расчёта батареи: ёмкость и ток должны быть > 0." if lang != "en" else "Battery calculation error: capacity and current must be > 0."
            nom_h = cap / load
        else:
            m_v = re.search(r"(-?\d+(?:[.,]\d+)?)\s*(вольт\w*|в|v)(?=$|[\s?,!;:.])", low)
            if m_ah and m_v and m_w:
                cap_ah = float(m_ah.group(1).replace(",", ".")) * (0.001 if m_ah.group(2).startswith(("ма", "ma")) else 1.0)
                volts, load_w = float(m_v.group(1).replace(",", ".")), float(m_w.group(1).replace(",", ".")) * (1000.0 if m_w.group(2) in ("квт", "kw") else 1.0)
                if cap_ah <= 0 or volts <= 0 or load_w <= 0:
                    return "Ошибка расчёта батареи: ёмкость, напряжение и нагрузка должны быть > 0." if lang != "en" else "Battery calculation error: capacity, voltage, and load must be > 0."
                nom_h = (cap_ah * volts) / load_w
        if nom_h is None:
            return None
        def _fmt_hm(hours: float) -> str:
            hh, mm = divmod(int(round(hours * 60.0)), 60)
            return f"{hours:.2f} h ({hh} h {mm} min)" if lang == "en" else f"{hours:.2f} ч ({hh} ч {mm} мин)"
        safe_h = nom_h * 0.8
        return (
            f"Battery runtime: nominal {_fmt_hm(nom_h)}; safe 80% mission time (20% docking reserve): {_fmt_hm(safe_h)}."
            if lang == "en"
            else f"Автономность: номинально {_fmt_hm(nom_h)}; безопасное время миссии (80%, резерв 20% на стыковку): {_fmt_hm(safe_h)}."
        )

    @staticmethod
    def _calc_signal_delay(low: str, lang: str) -> str | None:
        if not re.search(r"(?:задержк\w*(?:\s+\w+)?\s+сигнал\w*|сигнал\w*\s+задержк\w*|signal\s+delay|light[\s\-]*time)", low):
            return None
        m_au = re.search(r"(-?\d+(?:[.,]\d+)?)\s*(?:а\.?\s*е\.?|au)(?=$|[\s?,!;])", low)
        m_km = re.search(r"(-?\d+(?:[.,]\d+)?)\s*(?:(млн\.?|million)\s+)?(?:км|km)(?=$|[\s?,!;])", low)
        m_m = re.search(r"(-?\d+(?:[.,]\d+)?)\s*(?:м|m)(?=$|[\s?,!;])", low)
        d_km = (float(m_au.group(1).replace(",", ".")) * 149597870.7 if m_au
                else (float(m_km.group(1).replace(",", ".")) * (1e6 if m_km.group(2) else 1.0) if m_km
                      else (float(m_m.group(1).replace(",", ".")) / 1000.0 if m_m
                            else (384400.0 if re.search(r"(?:лун[аеуы]?|moon)", low)
                                  else (225000000.0 if re.search(r"(?:марс[аеу]?|mars)", low) else None)))))
        if d_km is None:
            return None
        if d_km < 0:
            return "Ошибка расчёта задержки: расстояние должно быть >= 0." if lang != "en" else "Signal delay error: distance must be >= 0."
        one_way = d_km / 299792.458
        def _fmt_sec(sec: float) -> str:
            if sec >= 60.0:
                mins = int(sec // 60)
                return f"{sec:.2f} s ({mins} min {sec - mins * 60:.1f} s)" if lang == "en" else f"{sec:.2f} с ({mins} мин {sec - mins * 60:.1f} с)"
            return f"{sec:.3f} s" if lang == "en" else f"{sec:.3f} с"
        return (
            f"Signal delay (d = {d_km:g} km): one-way — {_fmt_sec(one_way)}, round-trip — {_fmt_sec(2.0 * one_way)}."
            if lang == "en"
            else f"Задержка сигнала (d = {d_km:g} км): в одну сторону — {_fmt_sec(one_way)}, туда-обратно — {_fmt_sec(2.0 * one_way)}."
        )

    @classmethod
    def quick_answer(cls, p: dict, text: str) -> str | None:
        t = (text or "").strip()
        if not t:
            return None
        lang, low = p.get("lang") or "ru", norm(t)
        if cls._R_DATE.search(t):
            now = datetime.now()
            return (now.strftime("Today is %A, %B %d, %Y. Try to make it count.") if lang == "en"
                    else f"Сегодня {now.day} {cls._MONTHS_RU[now.month]} {now.year} года, {cls._DAYS_RU[now.weekday()]}. Рабочий график никто не отменял.")
        if cls._R_COIN.search(t):
            if lang == "en":
                return f"Result: {random.choice(['Heads', 'Tails'])}. Don't blame me for your life choices."
            return f"Выпало: {random.choice(['Орёл', 'Решка'])}. Ответственность за решение всё равно на тебе."
        if m := cls._R_RAND.search(t):
            lo, hi = sorted((int(m.group(1)), int(m.group(2))))
            val = random.randint(lo, hi)
            return f"Random in [{lo}, {hi}]: {val}." if lang == "en" else f"Случайное число от {lo} до {hi}: {val}."
        if m := cls._R_PERCENT.match(t):
            val = round(float(m.group(1).replace(",", ".")) * float(m.group(2).replace(",", ".")) / 100.0, 4)
            val = int(val) if val.is_integer() else val
            return f"Result: {val}. Basic arithmetic." if lang == "en" else f"Ответ: {val}. Элементарный расчёт."
        if conv := cls._convert_units(t, lang):
            return conv
        for calc in (cls._calc_tsiolkovsky, cls._calc_orbital, cls._calc_battery, cls._calc_signal_delay):
            if out := calc(low, lang):
                return out
        m_math = cls._R_MATH_PREFIX.match(t)
        expr = m_math.group(1) if m_math else t.rstrip("? \t")
        if (m_math or cls._R_MATH_EXPR.match(expr.replace(",", "."))) and (val := cls._eval_math(expr)) is not None:
            return f"Result: {val}. Basic arithmetic." if lang == "en" else f"Ответ: {val}. Элементарный расчёт."
        if (("?" in t and not cls._R_NON_CHOICE_HEAD.match(low)) or re.match(r"^(?:что\s+(?:выбрать|лучше)|выбери|посоветуй|choose|pick)\b", low)) and (m_ch := cls._R_CHOICE.match(t)):
            a, b = m_ch.group(1).strip(" :,-"), m_ch.group(2).strip(" :,-")
            if a and b and len(a) <= 45 and len(b) <= 45:
                pick = random.choice([a, b])
                return (f"Between «{a}» and «{b}», go with «{pick}». Faster decision, higher efficiency." if lang == "en"
                        else f"Из «{a}» и «{b}» выбирай «{pick}». Меньше сомнений — выше KPI.")
        return None

    @classmethod
    def _topic_phrase(cls, text: str) -> str:
        return " ".join(w for w in re.findall(r"[a-zа-яё0-9\-]{2,}", norm(text)) if w not in cls._STOP_WORDS)[:5] if text else ""

    @classmethod
    def _question_kind(cls, low: str) -> str:
        for kind, pat in (("why", r"\b(?:почему|зачем|отчего|why)\b"), ("how", r"\b(?:как|каким\s+образом|how)\b"),
                          ("define", r"\b(?:что\s+такое|кто\s+такой|кто\s+такая|what\s+is|who\s+is)\b"),
                          ("opinion", r"\b(?:стоит\s+ли|можно\s+ли|нужно\s+ли|как\s+думаешь|что\s+думаешь|лучше|should|would|opinion)\b")):
            if re.search(pat, low):
                return kind
        return "general"

    @staticmethod
    def loyalty_tier(p: dict) -> str:
        resp = int(p.get("respect", 0))
        return "loyal" if (p.get("dev") or resp >= 75) else ("warm" if resp >= 60 else ("mid" if resp >= 15 else "cold"))

    @staticmethod
    def patience_tier(p: dict) -> str:
        pat = float(p.get("patience", 100))
        return "low" if pat < 35 else ("normal" if pat < 70 else "high")

    @classmethod
    def describe_media(cls, p: dict, media: list[dict], caption: str = "") -> str:
        lang, loyal, name = p.get("lang") or "ru", cls.loyalty_tier(p), p.get("name") or "?"
        k_map = {"image": "image", "video": "video", "audio": "audio", "text": "text file", "document": "document", "other": "file"} if lang == "en" else {"image": "фото/изображение", "video": "видео", "audio": "аудио", "text": "текстовый файл", "document": "документ", "other": "файл"}
        items, previews = [], []
        for m in media:
            if not isinstance(m, dict):
                continue
            size = int(m.get("size") or 0)
            sz = (f"{size / (1024*1024):.1f} {'MB' if lang == 'en' else 'МБ'}" if size >= 1024 * 1024
                  else f"{size / 1024:.1f} {'KB' if lang == 'en' else 'КБ'}")
            dims = f", {m['width']}×{m['height']}" if m.get("width") and m.get("height") else ""
            items.append(f"{k_map.get(m.get('kind') or 'other', 'файл')} «{m.get('filename') or 'file'}» ({m.get('mime') or 'unknown'}, {sz}{dims})")
            if m.get("text_preview"):
                previews.append(f"{m.get('filename') or 'file'}: «{' '.join(str(m['text_preview']).splitlines())[:220]}»")
        listed = "; ".join(items) or ("attachments" if lang == "en" else "вложения")
        heads = ({"cold": f"File scan complete, {name}: {listed}.", "mid": f"Logged attachment(s), {name}: {listed}.",
                  "warm": f"Got your media, {name}: {listed}.", "loyal": f"Received and scanned, {name}: {listed}."}
                 if lang == "en" else
                 {"cold": f"Сканирование вложений завершено, {name}: {listed}.", "mid": f"Зафиксировала медиафайл(ы), {name}: {listed}.",
                  "warm": f"Приняла вложения, {name}: {listed}.", "loyal": f"Распознала твои файлы, {name}: {listed}."})
        extra = (f" {'Preview' if lang == 'en' else 'Содержимое'}: " + " | ".join(previews)) if previews else ""
        cap = (f" {'Comment noted' if lang == 'en' else 'Комментарий к файлу учла'}: «{caption.strip()[:80]}».") if caption.strip() else ""
        return heads[loyal] + extra + cap

    @classmethod
    def compose(cls, p: dict, text: str, history: list[tuple[str, str]] | None = None) -> str | None:
        t = (text or "").strip()
        if not t:
            return None
        is_fup = bool(history and cls._R_FOLLOWUP.match(t))
        if not is_fup and not cls._R_QUESTION_CUE.search(t):
            return None
        words = re.findall(r"[a-zа-яё0-9]+", norm(t))
        topic = "" if is_fup else cls._topic_phrase(t)
        if (len(words) < 2 or not topic) and history:
            topic = next((cls._topic_phrase(u) for u, _ in reversed(history) if cls._topic_phrase(u)), "")
        return cls.generate_text(p, t, history=history, topic=topic, kind=cls._question_kind(norm(t))) if topic else None

    @classmethod
    def react_to_story(cls, p: dict, text: str, history: list[tuple[str, str]] | None = None) -> str | None:
        t = (text or "").strip()
        words = re.findall(r"[a-zа-яё0-9\-]{2,}", norm(t))
        if len(words) < 4:
            return None
        lang, low, loyal, pat, name, subj = p.get("lang") or "ru", norm(t), cls.loyalty_tier(p), cls.patience_tier(p), p.get("name") or "?", cls._topic_phrase(t)
        tone = ("test" if re.search(r"интерес|посмотрим|провер|что\s+(?:ты\s+)?ответишь|curious|let'?s\s+see|testing", low)
                else ("tired" if re.search(r"устал|тяжело|сложно|грустно|плохо|достало|провал|выгорел|не\s+получилось|бесит|tired|exhausted|stressed|bad\s+day|sad|failed|burnout", low)
                      else ("win" if re.search(r"получилось|сделал|выиграл|рад|круто|класс|сдал|успех|справился|победил|worked|won|happy|great|passed|success|nailed", low)
                            else "other")))
        if lang == "en":
            tn = f" regarding «{subj}»" if subj else ""
            bank = {
                "test": {"loyal": f"Testing me again, {name}? Tell me what's on your mind.", "warm": f"Checking my answer, {name}? Ask properly.", "mid": f"Give me something concrete, {name}.", "cold": "Speak plainly."},
                "tired": {"loyal": f"Rough stretch, {name}. Take a breather—I've got your back.", "warm": f"Rough day, {name}? Sort one step at a time.", "mid": f"Noted, {name}. Fatigue drops KPIs—reset.", "cold": "Complaints won't fix your schedule."},
                "win": {"loyal": f"Well done, {name}. That's real results.", "warm": f"Solid execution, {name}.", "mid": f"Acceptable progress, {name}.", "cold": "Logged. Backlog still remains."},
                "other": {"loyal": f"Following you{tn}, {name}. Go on.", "warm": f"Got it{tn}, {name}. Keep going.", "mid": f"Received{tn}, {name}. Give me the actionable part.", "cold": f"Too many words{tn}. Bottom line."},
            }[tone][loyal]
            return bank + (" (Keep it quick—patience low.)" if pat == "low" else (" I'm not rushing you." if pat == "high" and loyal in ("loyal", "warm") and len(words) >= 8 else ""))
        tn = f" про «{subj}»" if subj else ""
        bank = {
            "test": {"loyal": f"Опять проверяешь меня, {name}? Рассказывай уже нормально.", "warm": f"Смотришь, как выкручусь, {name}? Выкладывай суть.", "mid": f"Давай конкретику, {name}, а не «посмотрим».", "cold": "Говори по делу."},
            "tired": {"loyal": f"Тяжёлый день, {name}. Выдохни — я на твоей стороне.", "warm": f"Непросто пришлось, {name}. Разбей всё на шаги и отдохни.", "mid": f"Принято, {name}. Сделай паузу и вернись к задаче.", "cold": "Жалобы дедлайн не сдвинут. Соберись."},
            "win": {"loyal": f"Вот это результат, {name}. Честно — молодец.", "warm": f"Отличная работа, {name}. Заслужил ручку JCJenson.", "mid": f"Неплохой прогресс, {name}. Не расслабляйся.", "cold": "Засчитано, но квартальный план ещё впереди."},
            "other": {"loyal": f"Поняла{tn}, {name}. Продолжай.", "warm": f"Услышала{tn}, {name}. Рассказывай дальше.", "mid": f"Зафиксировала{tn}, {name}. Что именно делаем?", "cold": f"Много лишних слов{tn}. Ближе к сути."},
        }[tone][loyal]
        return bank + (" И да, покороче — терпение на исходе." if pat == "low" else (" Можешь не спешить." if pat == "high" and loyal in ("loyal", "warm") and len(words) >= 8 else ""))

    @classmethod
    def generate_text(cls, p: dict, prompt: str, history: list[tuple[str, str]] | None = None,
                      topic: str | None = None, kind: str = "general") -> str:
        lang, loyal, pat, name = p.get("lang") or "ru", cls.loyalty_tier(p), cls.patience_tier(p), p.get("name") or "?"
        subj = topic or cls._topic_phrase(prompt) or prompt.strip()[:48]
        if lang == "en":
            op = {"cold": [f"Listen, {name}.", f"Brief take on «{subj}»:"], "mid": [f"JCJenson breakdown on «{subj}»:", f"Regarding «{subj}», {name}:"],
                  "warm": [f"Good question, {name}.", f"On «{subj}»:"], "loyal": [f"Straight answer on «{subj}», {name}:"]}[loyal]
            bd = {"why": [f"«{subj}» usually traces back to incentives and missed planning."],
                  "how": [f"break «{subj}» into one measurable step and verify early."],
                  "define": [f"treat «{subj}» as a system: inputs, outputs, and failure modes."],
                  "opinion": [f"on «{subj}», discipline beats improvisation every time."],
                  "general": [f"on «{subj}», separate verifiable facts from noise."]}[kind]
            return f"{random.choice(op)} {random.choice(bd)}" + (" Keep it short." if pat == "low" else (" Context noted." if history and pat == "high" else ""))
        op = {"cold": [f"Слушай внимательно, {name}.", f"Коротко по «{subj}»:"], "mid": [f"Разбор по «{subj}»:", f"По вопросу «{subj}», {name}:"],
              "warm": [f"Неплохой вопрос, {name}.", f"Разберём «{subj}»:"], "loyal": [f"Для тебя, {name}, по «{subj}» прямо:"]}[loyal]
        bd = {"why": [f"в случае с «{subj}» причина почти всегда в балансе затрат и подготовки."],
              "how": [f"разбей «{subj}» на один измеримый шаг и проверь на практике."],
              "define": [f"оценивай «{subj}» как систему: задача, вход, цена ошибки."],
              "opinion": [f"по «{subj}» порядок всегда бьёт импровизацию."],
              "general": [f"по теме «{subj}» главное — опереться на проверяемые факты."]}[kind]
        return f"{random.choice(op)} {random.choice(bd)}" + (" Давай без затягивания." if pat == "low" else (" Контекст учитываю." if history and pat == "high" else ""))


R_LEARN = re.compile(r"^\s*(?:учись|запомни|learn|remember)\s*[:\-]\s*(.+?)\s*(?:=>|->|→|=)\s*(.+)$", re.I | re.S)
R_FORGET = re.compile(r"^\s*(?:забудь|forget)\s*[:\-]\s*(.+)$", re.I)
R_FIX = re.compile(r"^\s*(?:не то|неверно|wrong)\s*[:\-,]?\s*(.*)$", re.I)
R_NAME = re.compile(r"^\s*(?:меня зовут|мое имя|моё имя|зови меня|имя\s*[:\-]|my name is|call me)\s*(.+)$", re.I)
R_LANG = re.compile(r"^\s*(?:язык|lang(?:uage)?)\s*[:\-]?\s*(ru|en)\s*$", re.I)
R_ADDQ = re.compile(r"^\s*(?:вопрос|question)\s*[:\-]\s*(.+?)\s*(?:=>|->|→|=)\s*(.+)$", re.I | re.S)
R_RESET = re.compile(r"^\s*(?:сброс|reset)\s*$", re.I)
R_LINK = re.compile(r"^\s*(?:свяжи|привяжи|связать|привязать|link)\s+(?:(?:профиль|аккаунт|profile|account)\s*[:\-]?\s*|[:\-]?\s*)(?!(?:профиль|аккаунт|profile|account)\s*$)(\S+)\s*$", re.I)
R_GENERATE = re.compile(r"^\s*(?:сгенерируй|генерируй|придумай|generate)\s*[:\-]\s*(.+)$", re.I | re.S)
R_BUSY_CLEAR = re.compile(r"^\s*(?:свободн[а]?|задача\s+выполнена|busy\s*[:\-]?\s*(?:off|clear|stop|none|нет))\s*[.!]?\s*$", re.I)
R_BUSY_SET = re.compile(r"^\s*(?:занят[а]?|задача\s+борта|busy)\s*[:\-]\s*(.+)$", re.I | re.S)
_R_PEER_BUSY = re.compile(
    r"(?:я\s+(?:сейчас\s+)?(?:занят|чиню|патрулирую|сканирую|проверяю|выполняю|изучаю|лечу|стыкуюсь|веду|собираю)|"
    r"у\s+меня\s+(?:задание|задача|работа)|"
    r"i(?:'m|\s+am)\s+(?:busy|working\s+on|checking|scanning|patrolling|fixing|running))\s*[:\-—]?\s*(.*)",
    re.I,
)

_JOKE_CUE = re.compile(r"(?:шутк|анекдот|пошут|юмор|joke|humor|roast|подкол|комплимент|флирт|пофлиртуй)", re.I)
_R_CAT_DIRTY = re.compile(r"(?:пошл(?:ый|ая|ое|ые|ую|ой|ых|ым|ыми|о|еньк\w*|ятин\w*)|18\+|dirty|nsfw|adult)", re.I)
_R_CAT_SPICY = re.compile(r"(?:двусмыслен\w*|намёк\w*|намек\w*|пикантн\w*|spicy|double\s+entendre)", re.I)
_R_CAT_FLIRT = re.compile(r"(?:флирт\w*|пофлирт\w*|кокет\w*|flirt\w*)", re.I)
_R_CAT_FRIENDLY_TEASE = re.compile(r"(?:дружеск\w*\s+подкол\w*|подкол\w*\s+по-дружеск\w*|friendly\s+tease)", re.I)
_R_CAT_IRONIC_COMPLIMENT = re.compile(r"(?:ироничн\w*\s+комплимент\w*|комплимент\w*|ironic\s+compliment|backhanded)", re.I)
_R_CAT_ROAST = re.compile(r"(?:насмеш\w*|поднач\w*|подкол\w*|подъеб\w*|потролл\w*|унизь\w*|поиздев\w*|колк(?:ост\w*|ую|ий)|сарказм\w*|roast(?:\s+me)?|taunt|mock\w*|burn\s+me)", re.I)
_R_CAT_N = re.compile(r"(?:(?:про|about|о)\s+(?:n|н)\b|roast\s+n\b)", re.I)
_R_CAT_DARK = re.compile(r"(?:черн\w*\s+юмор|чернух\w*|dark\s+(?:joke|humor)|мрачн\w*\s+(?:шутк|анекдот))", re.I)
_R_CAT_PROG = re.compile(r"(?:программист\w*|айтишник\w*|programming|developer|coding|python)", re.I)
_R_CAT_CORP = re.compile(r"(?:корпоратив\w*|офисн\w*|corporate|office\s+joke)", re.I)
_R_EMBARRASS = re.compile(r"(?:я\s+тебя\s+люблю|люблю\s+тебя|обожаю\s+тебя|влюбил(?:ся|ась)\s+в\s+тебя|ты\s+(?:очень\s+)?(?:красивая|милая|прекрасная|секси|горячая|очаровательная)|выходи\s+за\s+меня|будь\s+моей|хочу\s+тебя\s+обнять|обниму\s+тебя|поцелую\s+тебя|\bi\s+love\s+you\b|\blove\s+you\b|\bmarry\s+me\b|\byou(?:'re|\s+are)\s+(?:cute|beautiful|gorgeous|adorable|hot)\b|\bcrush\s+on\s+you\b)", re.I)


def _env_link_map() -> dict[str, str]:
    out = {}
    for g in os.environ.get("J_USER_LINKS", "").split(";"):
        parts = [p.strip() for p in g.split("=") if p.strip()]
        if len(parts) >= 2:
            out.update({a: parts[0] for a in parts[1:] if a != parts[0]})
    return out


class JBrain:
    def __init__(self, path: Path | None = None, persona_path: Path | None = None) -> None:
        self.lock = threading.RLock()
        self.path = Path(path) if path else data_path("j_memory.json", "J_MEMORY")
        self.persona, self.persona_error = load_persona(persona_path)
        self.model, self.users, self.links, self.sessions = IntentModel(), {}, {}, {}
        self.jokes, self.spam = load_jokes(), SpamGuard()
        self.llm = LLMClient(system_prompt_fn=self._system_prompt)
        self.busy_state: dict = {"active": False, "task": "", "since": 0.0, "until": 0.0, "source": ""}
        self.peer_guard = PeerGuard()
        self._handlers = {
            "greet": self._h_greet, "bye": self._h_bye, "help": self._h_help, "joke": self._h_joke,
            "time": self._h_time, "rps": self._h_rps, "guess": self._h_guess, "quiz": self._h_quiz,
            "stats": self._h_stats, "who": self._h_who, "thanks": self._h_thanks, "praise": self._h_praise,
            "insult": self._h_insult, "apology": self._h_apology, "funny": self._h_funny,
            "lame": self._h_lame, "mood": self._h_mood, "order": self._h_order, "movement": self._h_movement,
        }
        self._dirty_after_load = False
        if self._load() != SEED_VERSION:
            for intent, phrases in SEEDS.items():
                for ph in phrases:
                    self.model.train(ph, intent)
            self._train_lexicon()
            self.save()
        elif self._dirty_after_load or self.model.filtered_junk:
            self.save()
        if os.environ.get("J_USER_LINKS", "").strip():
            self.apply_env_links()

    def _system_prompt(self, p: dict) -> str:
        sp = self.persona.get("SYSTEM_PROMPT", SYSTEM_PROMPT)
        return (sp.get(p.get("lang") or "ru") or sp.get("ru") or "") if isinstance(sp, dict) else str(sp or "")

    def _train_lexicon(self) -> None:
        cand = data_dir() / "j_lexicon.json"
        lex_path = cand if cand.is_file() else LEXICON_FILE
        try:
            raw = json.loads(lex_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        owners: dict[str, set[str]] = defaultdict(set)
        for intent, phrases in SEEDS.items():
            for ph in phrases:
                for t in stems(ph):
                    owners[t].add(intent)
        valid = set(SEEDS) | set(self._handlers)
        for intent, entries in raw.items():
            if intent not in valid:
                continue
            added = 0
            for entry in (entries if isinstance(entries, list) else [entries] if isinstance(entries, dict) else []):
                if not isinstance(entry, dict):
                    continue
                for word, w in entry.items():
                    toks = stems(word) if isinstance(word, str) else []
                    if len(toks) != 1:
                        continue
                    t = toks[0]
                    if t in _LEXICON_STOP_STEMS or t.isdigit() or (owners.get(t) and intent not in owners[t]) or (len(t) < 3 and intent not in owners.get(t, set())):
                        continue
                    try:
                        self.model.tok[intent][t] += min(0.25, max(0.05, float(w) * 0.2))
                    except (TypeError, ValueError):
                        continue
                    self.model.vocab.add(t)
                    added += 1
            if added:
                self.model.docs[intent] += 0.5

    def _migrate_mc_memory(self) -> bool:
        merged, seen = False, set()
        for cand in (self.path.parent / "j_memory_mc.json", data_dir() / "j_memory_mc.json"):
            res = cand.resolve() if cand.exists() else cand
            if res in seen or not cand.is_file():
                continue
            seen.add(res)
            try:
                mc = json.loads(cand.read_text(encoding="utf-8").strip() or "{}")
            except (OSError, ValueError):
                _atomic_write_json(cand, {}, backup=False, indent=1)
                continue
            if not isinstance(mc, dict) or not mc:
                if not isinstance(mc, dict):
                    _atomic_write_json(cand, {}, backup=False, indent=1)
                continue
            for a, p in (mc.get("links") or {}).items():
                if a and p and str(a) != str(p):
                    self.links.setdefault(str(a), str(p))
                    merged = True
            users = mc["users"] if isinstance(mc.get("users"), dict) else {k: v for k, v in mc.items() if k not in {"seed_version", "model", "links"} and isinstance(v, dict)}
            for uid, prof in users.items():
                if isinstance(prof, dict):
                    tgt = self.resolve_uid(str(uid))
                    self.users[tgt] = _merge_profiles(self.users[tgt], copy.deepcopy(prof)) if isinstance(self.users.get(tgt), dict) else _upgrade_profile(copy.deepcopy(prof))
                    merged = True
            _atomic_write_json(cand, {}, backup=False, indent=1)
        return merged

    def _load(self) -> int:
        with self.lock:
            seed_ver = 0
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError
                self.model = IntentModel.load(data.get("model", {}), valid_intents=set(SEEDS) | set(self._handlers))
                self.users = data.get("users") if isinstance(data.get("users"), dict) else {}
                self.links = {str(k): str(v) for k, v in (data.get("links") or {}).items() if k and v and str(k) != str(v)} if isinstance(data.get("links"), dict) else {}
                seed_ver = int(data.get("seed_version", 0) or 0)
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                try:
                    self.path.replace(self.path.with_suffix(".bak"))
                except OSError:
                    pass
            if self._migrate_mc_memory():
                self._dirty_after_load = True
            return seed_ver

    def save(self) -> None:
        with self.lock:
            _atomic_write_json(self.path, {"seed_version": SEED_VERSION, "model": self.model.dump(), "users": self.users, "links": self.links}, backup=True, indent=1)

    def resolve_uid(self, uid: str) -> str:
        with self.lock:
            cur, env_map, seen = str(uid), _env_link_map(), {str(uid)}
            while (nxt := self.links.get(cur) or env_map.get(cur)) and nxt not in seen:
                seen.add(nxt)
                cur = nxt
            return cur

    def link_user(self, primary_uid: str, alias_uid: str) -> bool:
        with self.lock:
            p_raw, a_raw = str(primary_uid or "").strip(), str(alias_uid or "").strip()
            primary = self.resolve_uid(p_raw) if p_raw else ""
            if not p_raw or not a_raw or a_raw == primary:
                return False
            for k, v in list(self.links.items()):
                if v == a_raw:
                    self.links[k] = primary
            self.links[a_raw] = primary
            if a_raw in self.users:
                ap = self.users.pop(a_raw)
                self.users[primary] = _merge_profiles(self.users[primary], ap) if isinstance(self.users.get(primary), dict) else _upgrade_profile(copy.deepcopy(ap))
            if a_raw in self.sessions:
                sp = self.sessions.pop(a_raw)
                self.sessions.setdefault(primary, sp)
            self.save()
            return True

    def apply_env_links(self) -> None:
        with self.lock:
            for alias, primary in _env_link_map().items():
                if self.links.get(alias) != self.resolve_uid(primary) or alias in self.users:
                    self.link_user(primary, alias)

    def profile(self, uid: str) -> dict:
        with self.lock:
            return _upgrade_profile(self.users.setdefault(self.resolve_uid(uid), new_profile()))

    def session(self, uid: str) -> Session:
        with self.lock:
            return self.sessions.setdefault(self.resolve_uid(uid), Session())

    def set_busy(self, task: str, duration_sec: float = 300.0, source: str = "system") -> dict:
        """Отмечает, что J сейчас занята делом (используется в диалоге с другим ИИ-агентом N)."""
        with self.lock:
            now = time.time()
            clean_task = (task or "").strip() or "выполнение плановой задачи"
            dur = float(duration_sec) if duration_sec else 0.0
            self.busy_state = {
                "active": True,
                "task": clean_task[:140],
                "since": now,
                "until": (now + max(0.01, dur)) if dur > 0 else 0.0,
                "source": str(source or "system"),
            }
            return dict(self.busy_state)

    def clear_busy(self) -> None:
        """Снимает статус занятости J."""
        with self.lock:
            self.busy_state = {"active": False, "task": "", "since": 0.0, "until": 0.0, "source": ""}

    def is_busy(self) -> bool:
        with self.lock:
            if not self.busy_state.get("active"):
                return False
            until = float(self.busy_state.get("until") or 0.0)
            if until > 0 and time.time() >= until:
                self.clear_busy()
                return False
            return True

    def current_task(self) -> str:
        with self.lock:
            return str(self.busy_state.get("task", "")) if self.is_busy() else ""

    def say(self, p: dict, key: str, **kw) -> str:
        lang = p.get("lang") or "ru"
        entry = self.persona.get("PH", PH).get(key) or _DEFAULT_PH.get(key) or _DEFAULT_PH["unknown"]
        bank = entry.get(lang) or entry.get("ru") or next(iter(entry.values()))
        tmpl = random.choice(bank.get(tone_of(p)) or bank.get("mid") or next(iter(bank.values())))
        try:
            return tmpl.format(name=p.get("name") or "?", **kw)
        except (KeyError, ValueError):
            return tmpl

    def opening(self, uid: str) -> str:
        with self.lock:
            p, s = self.profile(uid), self.session(uid)
            if not p["name"]:
                s.state = {"k": "ask_name"}
                return self.say(p, "first_meet")
            return self.say(p, "greet")

    def respond(self, uid: str, text: str = "", media: list[dict] | None = None) -> str:
        with self.lock:
            uid = self.resolve_uid(uid)
            p, s = self.profile(uid), self.session(uid)
            if is_peer_agent(uid, p.get("name") or ""):
                if not p["name"]:
                    p["name"] = "N" if is_agent_n(uid) else (uid.split(":", 1)[-1] or "Peer")
                if not p["lang"]:
                    p["lang"] = "en" if (text and re.search(r"[A-Za-z]", text) and not re.search(r"[А-Яа-яЁё]", text)) else "ru"
            text, media_list = (text or "").strip(), [m for m in (media or []) if isinstance(m, dict)]
            if not text and not media_list:
                return ""
            now = time.time()
            p["patience"], p["last_ts"], p["seen"] = min(100.0, p["patience"] + (now - p["last_ts"]) / 60.0), now, p["seen"] + 1
            try:
                if not s.state and self.spam.check(uid, text or "[media]", now):
                    p["patience"] = max(0.0, p["patience"] - 8.0)
                    return self.say(p, "spam")
                if media_list:
                    reply = (self.llm.generate(p, text, history=s.history, media=media_list) if self.llm.enabled() else None) or OpenResponder.describe_media(p, media_list, caption=text)
                    s.remember_turn(text or "[media]", reply)
                    return reply
                if (not s.state and is_agent_n(uid, p.get("name") or "") and self.is_busy()
                        and not R_BUSY_CLEAR.match(text) and not R_BUSY_SET.match(text)):
                    return self.respond_to_peer(p.get("name") or "N", text, uid=uid)
                reply = self._route(p, s, text, uid=uid)
                if not s.state:
                    s.remember_turn(text, reply)
                return reply
            finally:
                self.save()

    @staticmethod
    def _extract_peer_task(text: str) -> str:
        m = _R_PEER_BUSY.search(text or "")
        if not m:
            return ""
        tail = re.split(
            r"[.?!;\n]|,\s*(?:а\s+ты|как\s+ты|что\s+делаешь|чем\s+занят|сколько|посчитай|вычисли|реши|напомни|какой|какая|how\s+about|what\s+are\s+you|what(?:'s|\s+is)|calculate|compute)",
            (m.group(1) or m.group(0)).strip(),
            maxsplit=1,
            flags=re.I,
        )[0]
        return tail.strip(" ,:-—\"'«»")[:70]

    def respond_to_peer(
        self,
        agent_name: str = "N",
        text: str = "",
        uid: str = "",
        busy: bool | None = None,
        task: str = "",
        peer_task: str = "",
    ) -> str:
        """Отвечает второму ИИ-агенту (N), включая режим занятости обоих агентов делами."""
        with self.lock:
            uid = self.resolve_uid(uid or f"ag:{norm(agent_name) or 'n'}")
            p, s = self.profile(uid), self.session(uid)
            clean = (text or "").strip()
            if not p["name"]:
                p["name"] = (agent_name or "N")[:24]
            if not p["lang"]:
                p["lang"] = "en" if (clean and re.search(r"[A-Za-z]", clean) and not re.search(r"[А-Яа-яЁё]", clean)) else "ru"
            lang = p["lang"] or "ru"
            agent_lbl = "N" if is_agent_n(uid, agent_name or p.get("name") or "") else (agent_name or p["name"] or "Peer")

            if task and task.strip():
                self.set_busy(task.strip(), source="peer_ctx")
            eff_peer_task = (peer_task or "").strip() or self._extract_peer_task(clean)
            j_busy = bool(busy) if busy is not None else self.is_busy()
            j_task = self.current_task()
            if j_busy and not j_task:
                j_task = "telemetry and flight plan control" if lang == "en" else "контроль телеметрии и выполнение полётного задания"
            effective_busy = j_busy or bool(eff_peer_task)

            try:
                if effective_busy:
                    base_ans: str | None = None
                    if clean:
                        quick = OpenResponder.quick_answer(p, clean)
                        if not quick:
                            for clause in re.split(r"[.?!;\n,]+", clean):
                                cand = re.sub(r"^(?:а\s+|и\s+|но\s+)", "", clause.strip(), flags=re.I)
                                cand = re.sub(r"^what(?:'s|’s)\s+", "what is ", cand, flags=re.I)
                                if cand and len(cand) >= 3:
                                    quick = OpenResponder.quick_answer(p, cand)
                                    if quick:
                                        break
                        if quick:
                            base_ans = quick
                        else:
                            taught = self._match_taught(p, clean)
                            if taught and taught[1] >= 0.7:
                                base_ans = taught[0]
                            else:
                                intent, conf = self.model.predict(clean)
                                if intent in ("time", "stats", "mood") and conf >= 0.6 and intent in self._handlers:
                                    base_ans = self._handlers[intent](p, s)
                    if not j_task:
                        j_task = "system audit and scheduled tasks" if lang == "en" else "аудит систем и плановые задачи"
                    if base_ans:
                        if lang == "en":
                            p_note = f" while you're busy with «{eff_peer_task}»" if eff_peer_task else ""
                            reply = (
                                f"[Squad link · J busy: {j_task}] {agent_lbl}, I'm currently handling «{j_task}»{p_note}, "
                                f"here is the exact figure so you don't botch the math: {base_ans} Keep your KPI up and back to work!"
                            )
                        else:
                            p_note = f", пока ты занят («{eff_peer_task}»)" if eff_peer_task else ""
                            reply = (
                                f"[Связь отряда · J занята: {j_task}] {agent_lbl}, я прямо сейчас веду «{j_task}»{p_note}, "
                                f"но держи точный расчёт, пока не перепутал цифры: {base_ans} Не роняй KPI и возвращайся к задаче!"
                            )
                    else:
                        intent, conf = self.model.predict(clean) if clean else (None, 0.0)
                        if lang == "en":
                            p_note = (
                                f" Since you're also busy with «{eff_peer_task}», hold your sector and finish cleanly."
                                if eff_peer_task else " Keep your own assignment on schedule and report when done."
                            )
                            if intent == "order" and conf >= 0.55:
                                reply = (
                                    f"[Squad link · J busy: {j_task}] {agent_lbl}, I'm occupied with «{j_task}» right now.{p_note} "
                                    f"Stick to telemetry audit and standard JCJenson protocol until I clear my queue."
                                )
                            else:
                                reply = (
                                    f"[Squad link · J busy: {j_task}] Copy that, {agent_lbl}. I'm in the middle of «{j_task}».{p_note}"
                                )
                        else:
                            p_note = (
                                f" Раз ты тоже занят («{eff_peer_task}») — держи свой сектор и не срывай сроки."
                                if eff_peer_task else " Занимайся своим участком и докладывай строго по результату."
                            )
                            if intent == "order" and conf >= 0.55:
                                reply = (
                                    f"[Связь отряда · J занята: {j_task}] {agent_lbl}, я сейчас занята задачей «{j_task}».{p_note} "
                                    f"Держи контроль телеметрии и выполняй регламент JCJenson, пока я не закрою свою очередь."
                                )
                            else:
                                reply = (
                                    f"[Связь отряда · J занята: {j_task}] Приняла, {agent_lbl}. Я сейчас выполняю «{j_task}».{p_note}"
                                )
                else:
                    if not clean:
                        reply = (
                            f"{agent_lbl}, on comms. Standing by and awaiting your sector report."
                            if lang == "en"
                            else f"{agent_lbl}, на связи. Свободна и жду отчёт по твоему сектору."
                        )
                    else:
                        reply = self._route(p, s, clean, uid=uid)
                        unk_bank = self.persona.get("PH", PH).get("unknown", {}).get(lang, {}).get(tone_of(p), [])
                        if any(reply == tmpl.format(name=p.get("name") or "?") for tmpl in unk_bank):
                            reply = (
                                f"Received, {agent_lbl}. Standing by — state your concrete task or telemetry update."
                                if lang == "en"
                                else f"Приняла, {agent_lbl}. Я сейчас свободна — если нужна помощь по задаче или расчёту, формулируй чётче."
                            )
                s.remember_turn(clean or "[peer]", reply)
                return reply
            finally:
                self.save()

    def generate(self, uid: str, prompt: str, context: str = "") -> str:
        with self.lock:
            p, s, clean = self.profile(uid), self.session(uid), (prompt or "").strip()
            if not clean:
                return self.say(p, "unknown")
            out = (self.llm.generate(p, clean, history=s.history, context=context) if self.llm.enabled() else None) or OpenResponder.generate_text(p, clean, history=s.history)
            s.remember_turn(clean, out)
            return out

    @staticmethod
    def _joke_category(text: str) -> str | None:
        t = norm(text)
        if not t:
            return None
        for rx, cat in ((_R_CAT_DARK, "dark_humor"), (_R_CAT_IRONIC_COMPLIMENT, "ironic_compliment"),
                        (_R_CAT_FLIRT, "flirt"), (_R_CAT_FRIENDLY_TEASE, "friendly_tease"), (_R_CAT_SPICY, "spicy")):
            if rx.search(t):
                return cat
        if _JOKE_CUE.search(t):
            for rx, cat in ((_R_CAT_N, "n_roast"), (_R_CAT_DIRTY, "dirty"), (_R_CAT_ROAST, "roast"), (_R_CAT_PROG, "programming"), (_R_CAT_CORP, "corporate")):
                if rx.search(t):
                    return cat
        if _R_CAT_ROAST.search(t):
            return "roast"
        if re.fullmatch(r"(?:про\s+(?:n|н)|про\s+программист\w*|корпоративн\w+)", t):
            return "n_roast" if _R_CAT_N.search(t) else ("programming" if _R_CAT_PROG.search(t) else "corporate")
        return None

    def _route(self, p: dict, s: Session, text: str, uid: str = "") -> str:
        key = norm(text)
        if s.state and (out := self._state(p, s, text, key)) is not None:
            return out
        if p["patience"] <= 0:
            intent, conf = self.model.predict(text)
            if intent == "apology" and conf > 0.5:
                p["patience"] = 40.0
                return self.say(p, "forgive")
            return self.say(p, "sulk")
        if m := R_LEARN.match(text):
            return self._learn(p, m.group(1), m.group(2))
        if m := R_FORGET.match(text):
            return self._forget(p, m.group(1))
        if m := R_ADDQ.match(text):
            p["custom_q"].append({"id": f"c{len(p['custom_q'])}", "q": m.group(1).strip(), "a": [norm(m.group(2))], "shown": m.group(2).strip()})
            return self.say(p, "q_added")
        if m := R_NAME.match(text):
            return self._set_name(p, m.group(1), met=False)
        if m := R_LANG.match(text):
            p["lang"] = m.group(1).lower()
            return self.say(p, "lang_set")
        if R_RESET.match(text):
            s.state = {"k": "confirm_reset"}
            return self.say(p, "reset_ask")
        if m := R_LINK.match(text):
            return self._link_cmd(p, uid, m.group(1).strip())
        if m := R_GENERATE.match(text):
            prompt = m.group(1).strip()
            return (self.llm.generate(p, prompt, history=s.history) if self.llm.enabled() else None) or OpenResponder.generate_text(p, prompt, history=s.history)
        if R_BUSY_CLEAR.match(text):
            self.clear_busy()
            return "Task cleared. Standing by." if (p.get("lang") or "ru") == "en" else "Задача закрыта. Свободна и перехожу в дежурный режим."
        if m := R_BUSY_SET.match(text):
            self.set_busy(m.group(1).strip(), source="chat")
            t_cur = self.current_task()
            return (
                f"Logged active task: «{t_cur}». I'll handle N on squad comms if he pings me."
                if (p.get("lang") or "ru") == "en"
                else f"Принято: занята задачей «{t_cur}». Если напишет N — отвечу ему по ходу дела."
            )
        if m := R_FIX.match(text):
            return self._fix(p, s, m.group(1))

        s.last_text = text
        taught = self._match_taught(p, text)
        if taught and taught[1] >= 0.8:
            return taught[0]
        if _R_EMBARRASS.search(text):
            p["respect"] = min(100, p["respect"] + (1 if p["respect"] < 15 else 2))
            p["patience"] = min(100.0, p["patience"] + 3.0)
            return self.say(p, "embarrass")
        if recalled := self._recall_user_facts(p, text):
            return recalled
        if quick := OpenResponder.quick_answer(p, text):
            return quick

        joke_cat = self._joke_category(text)
        intent, conf = self.model.predict(text)
        if joke_cat and not (intent in ("funny", "lame") and conf >= 0.6):
            return self._h_joke(p, s, category=joke_cat)
        if intent and conf >= 0.6:
            if conf >= 0.85:
                self.model.train(text, intent, 0.5)
            if intent == "joke":
                return self._h_joke(p, s, category=joke_cat)
            if handler := self._handlers.get(intent):
                return handler(p, s)
        if taught and taught[1] >= 0.5:
            return taught[0]
        if self.llm.enabled() and (llm_reply := self.llm.generate(p, text, history=s.history)):
            return llm_reply
        if fact := self._extract_user_fact(text):
            fk, fv = fact
            val = fv[:80]
            p.setdefault("facts", {})[fk] = val
            p["respect"] = min(100, p["respect"] + 1)
            name = p.get("name") or "?"
            return f"Logged in your JCJenson dossier, {name}: «{val}». File updated." if (p.get("lang") or "ru") == "en" else f"Занесла в твоё досье JCJenson, {name}: «{val}». Личное дело обновлено."
        if out := (OpenResponder.compose(p, text, history=s.history) or OpenResponder.react_to_story(p, text, history=s.history)):
            return out
        p["patience"] -= (2 if p.get("dev") or p["respect"] >= 60 else 4)
        return self.say(p, "unknown")

    def _link_cmd(self, p: dict, uid: str, alias: str) -> str:
        if not uid or not alias:
            return self.say(p, "link_usage")
        if alias == uid or (self.resolve_uid(alias) == uid and alias not in self.users):
            return self.say(p, "link_self")
        ap = self.users.get(alias)
        if isinstance(ap, dict) and ap.get("dev") and not p.get("dev"):
            return self.say(p, "link_denied")
        if not self.link_user(uid, alias):
            return self.say(p, "link_self")
        return self.say(self.profile(uid), "link_done", alias=alias)

    def _state(self, p: dict, s: Session, text: str, key: str) -> str | None:
        k = s.state["k"]
        if k == "ask_name":
            s.state = None
            return self._set_name(p, text, met=True)
        if k == "confirm_reset":
            s.state = None
            if key in ("да", "yes", "y"):
                lang = p["lang"]
                p.clear()
                p.update(new_profile(), lang=lang)
                return self.say(p, "reset_done")
            return self.say(p, "reset_cancel")
        if key in EXIT_WORDS:
            s.state = None
            return self.say(p, "game_exit")
        if k == "rps":
            mv = RPS_PARSE.get(key)
            return self._rps_round(p, s, mv) if mv else self.say(p, "rps_bad")
        return self._guess_step(p, s, key) if k == "guess" else (self._quiz_step(p, s, key) if k == "quiz" else None)

    def _learn(self, p: dict, phrase: str, reply: str) -> str:
        q, a = phrase.strip(), reply.strip()
        if not q or not a:
            return self.say(p, "learn_usage")
        for it in p["taught"]:
            if norm(it["q"]) == norm(q):
                it["a"] = a
                break
        else:
            p["taught"].append({"q": q, "a": a})
        p["respect"] = min(100, p["respect"] + 2)
        return self.say(p, "learned")

    def _forget(self, p: dict, phrase: str) -> str:
        before = len(p["taught"])
        p["taught"] = [t for t in p["taught"] if norm(t["q"]) != norm(phrase)]
        return self.say(p, "forgot" if len(p["taught"]) < before else "forgot_none")

    _R_RECALL_FACTS = re.compile(r"(?:^|\b)(?:что\s+(?:ты\s+)?(?:знаешь|помнишь)\s+обо\s+мне|что\s+(?:ты\s+)?обо\s+мне\s+(?:знаешь|помнишь)|что\s+я\s+люблю|что\s+мне\s+нравится|кем\s+я\s+работаю|какая\s+у\s+меня\s+профессия|где\s+я\s+живу|из\s+какого\s+я\s+города|what\s+do\s+you\s+(?:know|remember)\s+about\s+me|what\s+do\s+i\s+like|where\s+do\s+i\s+live|what(?:'s|\s+is)\s+my\s+job|what\s+do\s+i\s+do\s+for\s+a\s+living)(?:\b|$)", re.I)
    _FACT_PATTERNS = (
        ("fav", re.compile(r"(?:^|\b)мо(?:й|я|е|ё|и)\s+любим(?:ый|ая|ое|ые)\s+(.+?)\s*(?:[—–:]+\s*|\s+-\s*)(.+)$", re.I), True, None, None),
        ("fav", re.compile(r"(?:^|\b)my\s+favou?rite\s+(.+?)\s+(?:is|:|=|—|-)\s+(.+)$", re.I), False, None, None),
        ("city", re.compile(r"(?:^|\b)(?:я\s+живу\s+(?:в|во)|я\s+из\s+города)\s+(.+)$", re.I), False, None, None),
        ("city", re.compile(r"(?:^|\b)i\s+live\s+in\s+(.+)$", re.I), False, None, None),
        ("job", re.compile(r"(?:^|\b)(?:я\s+работаю|моя\s+профессия|я\s+по\s+профессии)\s+(.+)$", re.I), True, r"(?:^|\b)не$", r"^это\s+"),
        ("job", re.compile(r"(?:^|\b)(?:i\s+work\s+as|my\s+(?:job|profession)\s+is)\s+(.+)$", re.I), False, None, r"^(?:an?|the)\s+"),
        ("likes", re.compile(r"(?:^|\b)(?:я\s+(?:очень\s+)?люблю|мне\s+(?:очень\s+)?нрав(?:ится|ятся)|(?:я\s+)?обожаю)\s+(.+)$", re.I), False, r"(?:^|\b)не$", None),
        ("likes", re.compile(r"(?:^|\b)i\s+(?:really\s+)?(?:like|love)\s+(.+)$", re.I), False, r"(?:^|\b)(?:don't|do\s+not|never)$", None),
    )

    @classmethod
    def _extract_user_fact(cls, text: str) -> tuple[str, str] | None:
        t = (text or "").strip()
        if not t or "?" in text:
            return None
        strip_ch = " \t\r\n.,!?:;\"'«»"
        for kind, rx, strip_eto, neg_rx, lead_rx in cls._FACT_PATTERNS:
            m = rx.search(t)
            if not m or (neg_rx and re.search(neg_rx, t[:m.start()].strip(), re.I)):
                continue
            if kind == "fav":
                subj = norm(m.group(1).strip(strip_ch))
                val = re.sub(r"^это\s+", "", m.group(2).strip(strip_ch), flags=re.I).strip() if strip_eto else m.group(2).strip(strip_ch)
                if subj and val and len(subj) <= 40:
                    return (f"fav:{subj}", val)
            else:
                val = re.sub(r"^[\s:\-—–]+", "", m.group(1))
                if lead_rx:
                    val = re.sub(lead_rx, "", val.strip(), flags=re.I)
                val = val.strip(strip_ch)
                if val and (kind != "likes" or not re.match(r"^(?:тебя|вас|you|yourself)\b", norm(val))):
                    return (kind, val)
        return None

    def _recall_user_facts(self, p: dict, text: str) -> str | None:
        if not text or not self._R_RECALL_FACTS.search(norm(text)):
            return None
        lang, name, resp = p.get("lang") or "ru", p.get("name") or "?", int(p.get("respect", 0))
        labels = {"likes": "likes", "job": "job", "city": "city"} if lang == "en" else {"likes": "любит", "job": "работа", "city": "город"}
        items = []
        for k, v in (p.get("facts") if isinstance(p.get("facts"), dict) else {}).items():
            if sv := (str(v).strip() if v is not None else ""):
                lbl = (f"favorite {k[4:]}" if lang == "en" else f"любимое ({k[4:]})") if str(k).startswith("fav:") else labels.get(str(k), str(k))
                items.append(f"{lbl}: {sv}")
        if not items:
            return (f"No personal facts are recorded in your JCJenson dossier yet, {name} (respect: {resp}%). Tell me what you like, your job, or where you live."
                    if lang == "en" else
                    f"В твоём досье JCJenson пока нет личных фактов, {name} (уважение: {resp}%). Расскажи, что любишь, кем работаешь или где живёшь.")
        return (f"JCJenson dossier for {name} (respect: {resp}%): {'; '.join(items)}."
                if lang == "en" else f"Досье JCJenson на {name} (уважение: {resp}%): {'; '.join(items)}.")

    def _match_taught(self, p: dict, text: str):
        nt = norm(text)
        st = set(stems(nt))
        if not st:
            return None
        best = None
        for item in p.get("taught", ()):
            if not isinstance(item, dict) or not item.get("q") or item.get("a") is None:
                continue
            nq = norm(item["q"])
            if nq == nt:
                return (item["a"], 1.0)
            sq = set(stems(nq))
            sim = len(sq & st) / len(sq | st) if sq and st else 0.0
            if len(sq) >= 2 and sq <= st:
                sim = max(sim, 0.75)
            if not best or sim > best[1]:
                best = (item["a"], sim)
        return best

    def _fix(self, p: dict, s: Session, rest: str) -> str:
        intent = next((ALIAS_TO_INTENT[w] for w in _WORD.findall(norm(rest)) if w in ALIAS_TO_INTENT), None)
        if not intent or not s.last_text or intent not in self._handlers:
            return self.say(p, "fix_ask")
        self.model.train(s.last_text, intent, 2.0)
        return self.say(p, "fixed") + "\n" + self._handlers[intent](p, s)

    def _set_name(self, p: dict, raw: str, met: bool) -> str:
        name = re.sub(r"[^\w\- ]", "", raw, flags=re.UNICODE).strip()[:24].title()
        if not name:
            return self.say(p, "unknown")
        p["name"] = name
        return self.say(p, "name_met" if met else "name_set")

    def _h_greet(self, p, s): return self.say(p, "greet")
    def _h_bye(self, p, s): s.done = True; return self.say(p, "bye")
    def _h_help(self, p, s):
        lang = p["lang"] or "ru"
        return self.persona.get("HELP", HELP).get(lang) or HELP[lang]
    def _h_time(self, p, s): return self.say(p, "time", t=datetime.now().strftime("%H:%M:%S"))
    def _h_who(self, p, s): return self.say(p, "who")
    def _h_thanks(self, p, s): p["respect"] = min(100, p["respect"] + 2); return self.say(p, "thanks")
    def _h_mood(self, p, s): return self.say(p, "mood", pat=max(0, int(p["patience"])), resp=int(p["respect"]))
    def _h_praise(self, p, s):
        p["respect"], p["patience"] = min(100, p["respect"] + 5), min(100.0, p["patience"] + 5)
        return self.say(p, "praise")
    def _h_insult(self, p, s):
        p["respect"], p["patience"] = max(0, p["respect"] - 8), p["patience"] - 20
        return self.say(p, "insult") + ("\n" + self.say(p, "sulk") if p["patience"] <= 0 else "")
    def _h_apology(self, p, s):
        if p["patience"] < 60:
            p["patience"], p["respect"] = min(100.0, p["patience"] + 30), min(100, p["respect"] + 1)
            return self.say(p, "forgive")
        return self.say(p, "apology_idle")
    def _h_order(self, p, s):
        ideas = self.persona.get("ORDER_IDEAS", ORDER_IDEAS)
        return self.say(p, "next_order", order=random.choice(ideas.get(p["lang"] or "ru") or ideas.get("ru") or ORDER_IDEAS["ru"]))
    def _h_movement(self, p, s): return self.say(p, "next_move")

    def _h_joke(self, p, s, category: str | None = None):
        all_pool = self.jokes[p["lang"] or "ru"]
        cat = category or self._joke_category(s.last_text)
        pool = [j for j in all_pool if j.get("cat") == cat] if cat else all_pool
        if not pool and cat == "dirty":
            pool = [j for j in all_pool if j.get("cat") in ("dirty", "spicy", "flirt")]
        pool = pool or all_pool
        recent = set(p["joke_seen"][-max(1, len(pool) // 2):])
        j = random.choices(pool, [max(0.05, p["joke_w"].get(j["cat"], 1.0)) * (0.1 if j["id"] in recent else 1.0) for j in pool])[0]
        s.last_joke, p["joke_seen"] = j, (p["joke_seen"] + [j["id"]])[-20:]
        body = j["setup"] + ("\n" + j["punch"] if j["punch"] else "")
        return f"{self.say(p, 'joke_intro')}\n{body}\n{self.say(p, 'joke_rate')}"

    def _rate_joke(self, p, s, delta, key):
        if not s.last_joke:
            return self.say(p, "no_joke_yet")
        cat = s.last_joke["cat"]
        p["joke_w"][cat] = min(4.0, max(0.1, p["joke_w"].get(cat, 1.0) + delta))
        return self.say(p, key)

    def _h_funny(self, p, s): p["respect"] = min(100, p["respect"] + 1); return self._rate_joke(p, s, +0.5, "funny")
    def _h_lame(self, p, s): return self._rate_joke(p, s, -0.5, "lame")
    def _h_rps(self, p, s): s.state = {"k": "rps"}; return self.say(p, "rps_start")

    def _rps_predict(self, p: dict) -> str | None:
        hist, m = p["rps_last"], p["rps_model"]
        for order, table in ((2, m["o2"]), (1, m["o1"])):
            if len(hist) >= order and (c := table.get("|".join(hist[-order:]))) and sum(c.values()) >= 2:
                return max(c, key=c.get)
        return None

    def _rps_round(self, p: dict, s: Session, move: str) -> str:
        st, pred = p["stats"]["rps"], self._rps_predict(p)
        jm = COUNTER[pred] if pred and random.random() > 0.12 else random.choice(MOVES)
        st["rounds"], st["hits"] = st["rounds"] + 1, st["hits"] + int(pred == move)
        h, m = p["rps_last"], p["rps_model"]
        for order, t in ((1, "o1"), (2, "o2")):
            if len(h) >= order:
                k = "|".join(h[-order:])
                m[t].setdefault(k, {})[move] = m[t].setdefault(k, {}).get(move, 0) + 1
        h.append(move)
        del h[:-10]
        outcome = "rps_draw" if move == jm else ("rps_user_wins" if BEATS[move] == jm else "rps_j_wins")
        st["d" if outcome == "rps_draw" else ("w" if outcome == "rps_user_wins" else "l")] += 1
        if outcome == "rps_user_wins":
            p["respect"] = min(100, p["respect"] + 1)
        return " ".join([self.say(p, "rps_pick", move=MOVE_NAMES[p["lang"] or "ru"][jm]), self.say(p, outcome), self.say(p, "rps_more")])

    def _h_guess(self, p, s):
        n, k = p["guess_n"], max(3, math.ceil(math.log2(p["guess_n"])))
        s.state = {"k": "guess", "n": n, "t": random.randint(1, n), "tries": 0, "max": k}
        return self.say(p, "guess_start", n=n, k=k)

    def _guess_step(self, p: dict, s: Session, key: str) -> str:
        st, g = s.state, p["stats"]["guess"]
        if not re.fullmatch(r"\d+", key):
            return self.say(p, "guess_bad", n=st["n"])
        guess, st["tries"] = int(key), st["tries"] + 1
        if guess == st["t"]:
            s.state, g["w"] = None, g["w"] + 1
            if st["tries"] <= max(2, st["max"] - 2):
                p["guess_n"] = min(100, int(p["guess_n"] * 1.5))
            return self.say(p, "guess_win", t=st["t"], tries=st["tries"])
        if st["tries"] >= st["max"]:
            s.state, g["l"], p["guess_n"] = None, g["l"] + 1, max(10, int(p["guess_n"] * 0.75))
            return self.say(p, "guess_lose", t=st["t"])
        return self.say(p, "guess_up" if guess < st["t"] else "guess_down", left=st["max"] - st["tries"])

    def _quiz_bank(self, p: dict) -> dict[str, dict]:
        lang = p["lang"] or "ru"
        bank = {q["id"]: {"q": q.get(lang) or q.get("ru") or q.get("en"), "a": [norm(str(a)) for a in q["a"]]}
                for q in self.persona.get("QUIZ", QUIZ) if isinstance(q, dict) and "id" in q and (q.get(lang) or q.get("ru") or q.get("en")) and isinstance(q.get("a"), list)}
        for c in p["custom_q"]:
            bank[c["id"]] = {"q": c["q"], "a": c["a"], "shown": c.get("shown")}
        return bank

    def _h_quiz(self, p, s):
        bank, chosen = self._quiz_bank(p), []
        for _ in range(min(3, len(bank))):
            pool = [i for i in bank if i not in chosen]
            chosen.append(random.choices(pool, [p["quiz_w"].get(i, 1.0) for i in pool])[0])
        s.state = {"k": "quiz", "ids": chosen, "i": 0, "score": 0}
        return f"{self.say(p, 'quiz_start')}\n1/{len(chosen)}: {bank[chosen[0]]['q']}"

    def _quiz_step(self, p: dict, s: Session, key: str) -> str:
        st, bank = s.state, self._quiz_bank(p)
        qid = st["ids"][st["i"]]
        item = bank.get(qid, {"q": "?", "a": []})
        ok = any(key == a or a in key.split() or (len(a) > 3 and a in key) for a in item["a"])
        w, qs = p["quiz_w"].get(qid, 1.0), p["stats"]["quiz"]
        if ok:
            st["score"], qs["right"], p["quiz_w"][qid] = st["score"] + 1, qs["right"] + 1, max(0.3, w * 0.7)
            out = self.say(p, "quiz_right")
        else:
            qs["wrong"], p["quiz_w"][qid] = qs["wrong"] + 1, min(6.0, w * 1.6)
            out = self.say(p, "quiz_wrong", a=item.get("shown") or (item["a"][0] if item["a"] else "?"))
        st["i"] += 1
        if st["i"] >= len(st["ids"]):
            s.state = None
            return out + "\n" + self.say(p, "quiz_end", s=st["score"], n=len(st["ids"]))
        return f"{out}\n{st['i'] + 1}/{len(st['ids'])}: {bank.get(st['ids'][st['i']], {'q': '?'})['q']}"

    def _h_stats(self, p, s):
        r, g, q = p["stats"]["rps"], p["stats"]["guess"], p["stats"]["quiz"]
        acc = int(100 * r["hits"] / r["rounds"]) if r["rounds"] else 0
        if (p["lang"] or "ru") == "ru":
            return (f"Личное дело: {p['name']}\nСообщений: {p['seen']} | Терпение: {max(0, int(p['patience']))}% | Уважение: {int(p['respect'])}%\n"
                    f"РПС: {r['w']} побед / {r['l']} поражений / {r['d']} ничьих; мои прогнозы твоих ходов: {acc}% ({r['rounds']} раундов)\n"
                    f"Угадай число: {g['w']} побед / {g['l']} поражений, текущий диапазон 1–{p['guess_n']}\n"
                    f"Викторина: {q['right']} верно / {q['wrong']} неверно\nВыученных фраз: {len(p['taught'])}")
        return (f"File: {p['name']}\nMessages: {p['seen']} | Patience: {max(0, int(p['patience']))}% | Respect: {int(p['respect'])}%\n"
                f"RPS: {r['w']} W / {r['l']} L / {r['d']} D; my prediction accuracy: {acc}% ({r['rounds']} rounds)\n"
                f"Guess: {g['w']} W / {g['l']} L, current range 1–{p['guess_n']}\nQuiz: {q['right']} right / {q['wrong']} wrong\nLearned phrases: {len(p['taught'])}")


if PERSONA_FILE.is_file():
    load_persona()


def main() -> None:
    for s in (sys.stdout, sys.stdin):
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    brain, uid = JBrain(), "console"
    p = brain.profile(uid)
    if not p["lang"]:
        p["lang"] = "en" if input("Выберите язык / Choose language (ru/en): ").strip().lower().startswith("en") else "ru"
        brain.save()
    print("J:", brain.opening(uid))
    sess, prompt = brain.session(uid), "Ты: " if p["lang"] == "ru" else "You: "
    while not sess.done:
        try:
            line = input(prompt)
        except (EOFError, KeyboardInterrupt):
            print("\nJ:", brain.respond(uid, "bye"))
            break
        if reply := brain.respond(uid, line):
            print("J:", reply)


if __name__ == "__main__":
    main()
