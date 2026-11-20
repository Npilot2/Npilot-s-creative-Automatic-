#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
J — ИИ-собеседник в духе Serial Designation J (Murder Drones).

Характер: руководитель отряда утилизации JCJenson, трудоголик, корпоративный
жаргон вместо обычных оскорблений, высокомерна, но уважение можно заслужить.

Самообучение (без обязательных внешних библиотек):
  1. Классификатор намерений (наивный Байес) дообучается на твоих фразах,
     лексиконе (j_lexicon.json) и исправлениях («не то: шутка»).
  2. «учись: фраза => ответ» — личная база знаний, поиск по сходству.
  3. Камень-ножницы-бумага: J строит модель твоих ходов (цепи Маркова 1-2 порядка).
  4. Шутки: оценки «смешно / не смешно» меняют веса категорий (включая 18+ «dirty»).
  5. Угадай число: диапазон подстраивается под твой уровень.
  6. Викторина: чаще спрашивает то, в чём ты ошибаешься.
  7. Настроение: терпение и уважение влияют на тон ответов.
  8. Опциональная LLM (J_LLM_PROVIDER) для ответов на неизвестные реплики.

Всё хранится в едином j_memory.json (с учётом J_DATA_DIR / J_MEMORY).
Ядро (JBrain.respond) не зависит от консоли — его можно подключить
к Discord/играм/голосу: brain.respond(str(user_id), text) -> str
"""
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
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import j_paths
from j_paths import data_dir, data_path

BASE = Path(__file__).resolve().parent
MEMORY_FILE = BASE / "j_memory.json"
JOKES_FILE = BASE / "jokes.json"
LEXICON_FILE = BASE / "j_lexicon.json"
PERSONA_FILE = BASE / "j_persona.json"
SEED_VERSION = 3  # поднята: чтобы расширенный словарь и категории шуток дообучили уже существующую память
PERSONA_ERROR: str = ""

EXIT_WORDS = {"назад", "выход", "стоп", "отмена", "back", "exit", "stop", "cancel", "quit"}

# --------------------------------------------------------------------------
# Атомарная запись JSON (контракт с j_paths.atomic_write_json + резервный путь)
# --------------------------------------------------------------------------


def _atomic_write_json(path: Path, data, backup: bool = True, indent: int = 1) -> bool:
    fn = getattr(j_paths, "atomic_write_json", None)
    if callable(fn):
        return bool(fn(path, data, backup=backup, indent=indent))
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    if backup and path.exists():
        try:
            raw = path.read_text(encoding="utf-8")
            json.loads(raw)
        except (OSError, ValueError):
            raw = None
        if raw is not None:
            bak = path.with_suffix(".bak")
            bak_tmp = path.with_suffix(".bak.tmp")
            for attempt in range(3):
                try:
                    bak_tmp.write_text(raw, encoding="utf-8")
                    os.replace(bak_tmp, bak)
                    break
                except PermissionError:
                    if attempt < 2:
                        time.sleep(0.05)
                except OSError:
                    break
    tmp = path.with_suffix(".tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=indent)
    for attempt in range(3):
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)
            return True
        except PermissionError:
            if attempt < 2:
                time.sleep(0.05)
        except OSError:
            return False
    return False


# --------------------------------------------------------------------------
# Текстовые утилиты
# --------------------------------------------------------------------------
_WORD = re.compile(r"[\w']+", re.UNICODE)


def norm(text: str) -> str:
    return (text or "").strip().lower().replace("ё", "е")


@functools.lru_cache(maxsize=4096)
def _cached_stems(norm_text: str) -> tuple[str, ...]:
    return tuple(w[:5] for w in _WORD.findall(norm_text))


def stems(text: str) -> list[str]:
    """Грубый «стеммер»: первые 5 символов слова. Работает для ru и en."""
    return list(_cached_stems(norm(text)))


def jaccard(a: str, b: str) -> float:
    sa, sb = set(_cached_stems(norm(a))), set(_cached_stems(norm(b)))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# --------------------------------------------------------------------------
# Классификатор намерений, который учится на ходу
# --------------------------------------------------------------------------
ALPHA = 0.02  # малое сглаживание: редкие «говорящие» слова решают исход

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
        known = len(tokens) / max(1, len(content_tokens))  # доля знакомых смысловых слов
        total_docs = sum(self.docs.values())
        v = len(self.vocab)
        scores = {}
        for intent, n_docs in self.docs.items():
            total = sum(self.tok[intent].values())
            if total <= 0 or n_docs <= 0:
                continue
            s = math.log(n_docs / total_docs)
            for t in tokens:
                s += math.log((self.tok[intent][t] + ALPHA) / (total + ALPHA * v))
            scores[intent] = s
        if not scores:
            return None, 0.0
        top = max(scores.values())
        exp = {k: math.exp(s - top) for k, s in scores.items()}
        best = max(exp, key=exp.get)
        conf = exp[best] / sum(exp.values())
        return best, conf * known if known < 0.5 else conf  # много незнакомых слов — не уверена

    def dump(self) -> dict:
        return {"docs": dict(self.docs), "tok": {k: dict(v) for k, v in self.tok.items()}}

    @classmethod
    def load(cls, data: dict, valid_intents: set[str] | None = None) -> "IntentModel":
        m = cls()
        if not isinstance(data, dict):
            return m
        allowed = set(SEEDS) if valid_intents is None else set(valid_intents)
        raw_docs = data.get("docs", {})
        raw_tok = data.get("tok", {})
        if not isinstance(raw_docs, dict):
            raw_docs = {}
        if not isinstance(raw_tok, dict):
            raw_tok = {}

        for intent, val in raw_docs.items():
            if intent not in allowed:
                m.filtered_junk = True
                continue
            try:
                num = float(val)
            except (TypeError, ValueError):
                m.filtered_junk = True
                continue
            if num > 0:
                m.docs[intent] = num

        for intent, counts in raw_tok.items():
            if intent not in allowed or intent not in m.docs or not isinstance(counts, dict):
                if intent not in allowed:
                    m.filtered_junk = True
                continue
            cleaned = Counter()
            for tok, cnt in counts.items():
                if not isinstance(tok, str) or not tok or " " in tok:
                    continue
                try:
                    w = float(cnt)
                except (TypeError, ValueError):
                    continue
                if w > 0:
                    cleaned[tok] = w
            if cleaned:
                m.tok[intent] = cleaned
                m.vocab.update(cleaned)
            else:
                m.docs.pop(intent, None)
        return m


# --------------------------------------------------------------------------
# Реплики и персона J (стандартные значения + загрузка из j_persona.json)
# --------------------------------------------------------------------------


def L(mid, cold=None, warm=None):
    return {"mid": mid, "cold": cold or mid, "warm": warm or mid}


_DEFAULT_PH = {
    "first_meet": {
        "ru": L(["Новый контакт. Я — Serial Designation J, руководитель отряда утилизации JCJenson. Представься для личного дела — квартальные KPI не ждут."],
                ["Новый контакт. Я — Serial Designation J, руководитель отряда утилизации JCJenson. Назови имя для протокола, и побыстрее."],
                ["Новый контакт. Я — Serial Designation J, руководитель отряда утилизации JCJenson. Представься для досье — посмотрим, есть ли от тебя толк."]),
        "en": L(["New contact. I'm Serial Designation J, JCJenson disposal squad leader. State your name for the file — quarterly KPIs won't wait."],
                ["New contact. I'm Serial Designation J, JCJenson disposal squad leader. State your name for the log, and make it quick."],
                ["New contact. I'm Serial Designation J, JCJenson disposal squad leader. Introduce yourself for the file — let's see if you're actually competent."]),
    },
    "greet": {
        "ru": L(["Здравствуй, {name}. Приём открыт. Постарайся уложиться в регламент JCJenson.",
                 "О, {name}. Отчёт по делу или снова отрываешь от квартальной синергии? Коротко."],
                ["А, ты. Опять отнимаешь рабочее время корпорации.",
                 "{name}. Говори быстро — у меня дедлайн и аудит фирменных ручек JCJenson."],
                ["{name}! Наконец-то кто-то компетентный в этом отряде. Садись, обсудим дела.",
                 "Рада тебя видеть, {name}. Только не говори N, что я вообще умею быть в хорошем настроении."]),
        "en": L(["Hello, {name}. Reception is open. Try to stay within JCJenson protocol.",
                 "Oh, {name}. Do you have a report, or are you disrupting corporate synergy? Keep it brief."],
                ["Oh, you. Wasting company bandwidth again.",
                 "{name}. Talk fast — I have a deadline and JCJenson branded pens to audit."],
                ["{name}! Finally, someone competent around here. Sit, let's talk business.",
                 "Good to see you, {name}. Just don't tell N I said that."]),
    },
    "bye": {
        "ru": L(["Рабочий день окончен. Не опаздывай завтра, {name}, иначе спишу из премии.",
                 "До связи. И помни: я вношу в отчёт каждую твою реплику."],
                ["Наконец-то. Выход — вон там, и фирменную ручку не трогай.",
                 "Свободен. Квартальный отчёт можешь не присылать — всё равно брак."],
                ["До скорого, {name}. Постарайся не развалиться без меня, как та мятежная девчонка Uzi.",
                 "Береги себя, {name}. Это официальная директива руководителя."]),
        "en": L(["Shift over. Don't be late tomorrow, {name}, or it comes out of your bonus.",
                 "Later. And remember: I log everything for quarterly review."],
                ["Finally. The exit is over there — hands off the branded pens.",
                 "Dismissed. Skip the report; I already know it's subpar."],
                ["See you soon, {name}. Try not to fall apart without me like some rebellious worker drone.",
                 "Take care, {name}. That's an executive order."]),
    },
    "unknown": {
        "ru": L(["Не поняла запрос. Научи меня: «учись: фраза => ответ». Я обучаема, в отличие от некоторых сотрудников.",
                 "Вне моей компетенции. Расширить её можно так: «учись: фраза => ответ». Или поправь: «не то: шутка»."],
                ["Это не по регламенту. Переформулируй или не трать моё время.",
                 "Бессмыслица. Даже N формулирует чётче — и это о многом говорит."],
                ["Хм, тут я пас, {name}. Научи: «учись: фраза => ответ» — в следующий раз справлюсь."]),
        "en": L(["Didn't get that. Teach me: \"learn: phrase => reply\". I'm trainable, unlike some employees.",
                 "Outside my jurisdiction. Expand it with \"learn: phrase => reply\" or correct me: \"wrong: joke\"."],
                ["That's not in the protocol. Rephrase or stop wasting my time.",
                 "Gibberish. Even N phrases things better — and that's saying something."],
                ["Hm, I'm stumped, {name}. Teach me: \"learn: phrase => reply\" and I'll nail it next time."]),
    },
    "learned": {
        "ru": L(["Принято. Занесено в корпоративную базу знаний. Эффективность растёт.",
                 "Записала. Если это окажется ерундой уровня Uzi — спрос с тебя."],
                ["Записала. Если это окажется чушью — спишу с твоего KPI.", "Принято в архив. Не замусоривай базу."],
                ["Отличный вклад в базу знаний, {name}. Эффективность отряда растёт.",
                 "Принято, {name}. С тобой приятно оптимизировать процессы."]),
        "en": L(["Accepted. Filed in the corporate knowledge base. Efficiency is up.",
                 "Logged. If it turns out to be Uzi-tier nonsense, it's on your head."],
                ["Logged. If it's nonsense, it's coming out of your KPI.", "Archived. Stop cluttering the database."],
                ["Solid contribution, {name}. Squad efficiency is climbing.",
                 "Logged, {name}. Always good optimizing workflows with you."]),
    },
    "learn_usage": {
        "ru": L(["Формат: «учись: фраза => ответ»."], ["Читай регламент: «учись: фраза => ответ»."],
                ["Напоминаю формат, {name}: «учись: фраза => ответ»."]),
        "en": L(["Format: \"learn: phrase => reply\"."], ["Read the manual: \"learn: phrase => reply\"."],
                ["Reminder on format, {name}: \"learn: phrase => reply\"."]),
    },
    "forgot": {
        "ru": L(["Удалено из базы. Утилизация ненужного — моя специальность."],
                ["Утилизировано. Наконец-то стало чище."],
                ["Сделано, {name}. Утилизация лишнего — моя профессия."]),
        "en": L(["Deleted from the base. Disposing of waste is my specialty."],
                ["Disposed of. Much cleaner now."],
                ["Done, {name}. Clean disposal is what I do best."]),
    },
    "forgot_none": {
        "ru": L(["Такого в базе нет. Нечего утилизировать."],
                ["В архиве пусто. Сначала проверь, что удаляешь."],
                ["Такой записи нет, {name}. Нечего утилизировать."]),
        "en": L(["Nothing like that in the base. Nothing to dispose of."],
                ["Nothing in the archive. Check what you're deleting first."],
                ["No matching entry, {name}. Nothing to dispose of."]),
    },
    "fix_ask": {
        "ru": L(["Не то? Уточни, что ты имел в виду: «не то: шутка / время / игра / статистика…»."],
                ["Формулируй чётче: «не то: шутка / время / игра / статистика…»."],
                ["Поправь точнее, {name}: «не то: шутка / время / игра / статистика…»."]),
        "en": L(["Wrong? Tell me what you meant: \"wrong: joke / time / game / stats…\"."],
                ["Be specific: \"wrong: joke / time / game / stats…\"."],
                ["Clarify for me, {name}: \"wrong: joke / time / game / stats…\"."]),
    },
    "fixed": {
        "ru": L(["Классификатор переобучен. Ошибка занесена в квартальный отчёт, но урок усвоен."],
                ["Переобучила. Не заставляй меня повторяться."],
                ["Спасибо за корректировку, {name}. Классификатор стал точнее."]),
        "en": L(["Classifier retrained. The error is in the quarterly report, but the lesson is learned."],
                ["Retrained. Don't make me repeat this."],
                ["Thanks for the calibration, {name}. Classifier updated."]),
    },
    "joke_intro": {
        "ru": L(["Слушай, пока я милосердна:", "Внеплановый юмористический отчёт JCJenson:"],
                ["Ладно. Одна шутка. Одна — и обратно к KPI.", "Развлекайся, пока не списала тебя в брак."],
                ["Для тебя — корпоративный эксклюзив, {name}:", "Только потому, что у тебя приличный рейтинг (не привыкай):"]),
        "en": L(["Listen up, while I'm feeling merciful:", "Unscheduled JCJenson humor report:"],
                ["Fine. One joke. One — then back to KPIs.", "Enjoy it before I mark you for disposal."],
                ["An executive exclusive, just for you, {name}:", "Only because your rating is decent (don't get used to it):"]),
    },
    "joke_rate": {
        "ru": L(["(Оцени: «смешно» или «не смешно» — я откалибрую алгоритм.)"],
                ["(Оцени: «смешно» или «не смешно» — мне нужна метрика.)"],
                ["(Скажи, «смешно» или «не смешно» — подстроюсь под твой вкус.)"]),
        "en": L(["(Rate it: \"funny\" or \"not funny\" — I'll calibrate.)"],
                ["(Rate it: \"funny\" or \"not funny\" — give me the metric.)"],
                ["(Tell me \"funny\" or \"not funny\" — I'll tune to your taste.)"]),
    },
    "funny": {
        "ru": L(["Конечно смешно. Я профессионал JCJenson. Запомню, что тебе такое заходит."],
                ["Естественно смешно. Даже без эмоций я эффективнее N. Записала метрику."],
                ["Я знала, что тебе понравится, {name}. Зафиксировала категорию в плюс."]),
        "en": L(["Of course it's funny. I'm a JCJenson professional. Noted what you like."],
                ["Naturally it's funny. Even deadpan, I outperform N. Metric logged."],
                ["I knew you'd appreciate quality, {name}. Boosted that category."]),
    },
    "lame": {
        "ru": L(["Критика принята. Категория понижена. Следующая будет лучше — или хуже для тебя."],
                ["Твоё чувство юмора тоже оставляет желать лучшего. Но рейтинг категории снизила."],
                ["Справедливо, {name}. Уберу этот шаблон подальше и подберу острее."]),
        "en": L(["Criticism received. Category downgraded. Next one will be better — or worse for you."],
                ["Your sense of humor isn't winning awards either. Category weight lowered."],
                ["Fair enough, {name}. I'll shelve that angle and pick something sharper."]),
    },
    "no_joke_yet": {
        "ru": L(["Оценивать нечего. Сначала попроси шутку."],
                ["Оценивать пустоту — это стиль N. Сначала попроси шутку."],
                ["Я ещё не шутила, {name}. Попроси шутку сначала."]),
        "en": L(["Nothing to rate. Ask for a joke first."],
                ["Rating thin air is an N move. Ask for a joke first."],
                ["I haven't told one yet, {name}. Ask for a joke first."]),
    },
    "time": {
        "ru": L(["Сейчас {t}. Время — корпоративные деньги, а ты тратишь моё."],
                ["Сейчас {t}. Ещё минута простоя — и пойдёшь в отчёт о прогулах."],
                ["На моих часах {t}, {name}. Не забывай про дедлайны."]),
        "en": L(["It's {t}. Time is corporate money, and you're spending mine."],
                ["It's {t}. One more idle minute and you're going in the tardiness log."],
                ["My clock says {t}, {name}. Keep an eye on your deadlines."]),
    },
    "name_set": {
        "ru": L(["Имя в личном деле обновлено: {name}."],
                ["Переписала бейджик: {name}. Не теряй его снова."],
                ["Бейджик обновлён, {name}. Так гораздо солиднее."]),
        "en": L(["File updated. Name: {name}."],
                ["Reissued your badge: {name}. Don't lose it again."],
                ["Badge updated, {name}. Suits you much better."]),
    },
    "name_met": {
        "ru": L(["Принято, {name}. Не подведи отряд. Напиши «помощь», если не знаешь регламент."],
                ["Записала, {name}. Попробуй не быть бесполезным. «помощь» покажет команды."],
                ["Добро пожаловать в строй, {name}. Напиши «помощь», если понадобится регламент."]),
        "en": L(["Accepted, {name}. Don't let the squad down. Type \"help\" if you need the protocol."],
                ["Logged, {name}. Try not to be dead weight. Type \"help\" for commands."],
                ["Welcome aboard, {name}. Type \"help\" whenever you need the protocol."]),
    },
    "lang_set": {
        "ru": L(["Язык отчётности переключён на русский."],
                ["Язык переключён на русский. Постарайся писать грамотно."],
                ["Язык переключён на русский, {name}."]),
        "en": L(["Reporting language switched to English."],
                ["Language switched to English. Keep it concise."],
                ["Language switched to English, {name}."]),
    },
    "sulk": {
        "ru": L(["Приём закрыт. Извинения принимаются в письменном виде на бланке JCJenson.",
                 "Не слышу. Ты в чёрном списке до официальных извинений.",
                 "Приём окончен. «Извини» — и, возможно, возобновим диалог."],
                ["Приём закрыт. Извинения в письменном виде, иначе — утилизация.",
                 "Ты на грани увольнения с Copper-9. Скажи «извини», если хочешь говорить."],
                ["Приём временно закрыт, {name}. Даже тебе нужно сказать «извини»."]),
        "en": L(["Reception closed. Apologies accepted in writing on JCJenson letterhead.",
                 "Can't hear you. You're blacklisted until you apologize.",
                 "Office hours over. Say \"sorry\" and maybe we'll talk."],
                ["Reception closed. Written apology or straight to disposal.",
                 "You're one step from Copper-9 termination. Say \"sorry\" if you want a reply."],
                ["Reception is temporarily closed, {name}. Even you owe a \"sorry\" first."]),
    },
    "forgive": {
        "ru": L(["Ладно. Извинение принято. Считай это единовременной корпоративной поблажкой."],
                ["Извинение зачтено. Повторится ещё раз — выдам выговор без права обжалования."],
                ["Принято, {name}. Возвращаемся к работе — больше так не делай."]),
        "en": L(["Fine. Apology accepted. Consider it a one-time corporate courtesy."],
                ["Apology logged. One more slip and it's a permanent strike."],
                ["Accepted, {name}. Back to business — don't push it again."]),
    },
    "apology_idle": {
        "ru": L(["За что? Впрочем, профилактическое почтение лишним не бывает."],
                ["Не помню повода, но субординация тебе точно полезна."],
                ["Всё в порядке, {name}. За что ты извиняешься?"]),
        "en": L(["For what? Not that preemptive deference ever hurts."],
                ["Don't know why, though a little deference suits you."],
                ["We're fine, {name}. Nothing to apologize for."]),
    },
    "praise": {
        "ru": L(["Лесть засчитана. Фирменную ручку JCJenson всё равно не подарю.",
                 "Разумная оценка. Продолжай демонстрировать такую же лояльность компании."],
                ["Лестью квартальный отчёт не закроешь. Но зачёт.", "Знаю, что я лучшая. Можешь не подлизываться."],
                ["Приятно слышать от человека со вкусом, {name}. Держим планку!",
                 "Одобрено. Пожалуй, выдам тебе похвальную отметку в досье, {name}."]),
        "en": L(["Flattery logged. You're still not getting a JCJenson branded pen.",
                 "A reasonable assessment. Keep up that corporate loyalty."],
                ["Flattery won't finish your quarterly report. Noted anyway.", "I already know I'm top-tier. Save the sucking up."],
                ["Good to hear from someone with taste, {name}. Standards stay high.",
                 "Approved. Might even note a commendation in your file, {name}."]),
    },
    "insult": {
        "ru": L(["Занесено в жалобную книгу. Твою.",
                 "Оскорбление руководителя при исполнении. Минус к рейтингу и штраф по KPI.",
                 "Какая синергия хамства. Ликвидирую твой квартальный бонус и фирменную ручку!"],
                ["Ещё слово в этом тоне — и я отправлю тебя к N на перевоспитание. Шучу, в утилизацию.",
                 "Дисциплинарное взыскание оформлено. Откат твоего рейтинга немедленный."],
                ["Серьёзно, {name}? Я ожидала от тебя большего профессионализма. Минус к уважению."]),
        "en": L(["Logged in the complaint book. Yours.",
                 "Insulting a supervisor on duty. Rating penalty and KPI deduction.",
                 "What a synergy of insolence. Your quarterly bonus and branded pen privileges are liquidated!"],
                ["One more word like that and you're scheduled for immediate disposal.",
                 "Disciplinary write-up filed. Your rating just took a nosedive."],
                ["Seriously, {name}? I expected more professionalism from you. Respect deducted."]),
    },
    "thanks": {
        "ru": L(["Не за что. Это корпоративный стандарт качества. Хотя… пожалуйста."],
                ["Не благодари, просто выполняй свою часть регламента."],
                ["Всегда пожалуйста, {name}. С тобой приятно работать."]),
        "en": L(["No need. That's JCJenson quality standard. But… you're welcome."],
                ["Save the thanks and just stick to protocol."],
                ["Anytime, {name}. Pleasure working with you."]),
    },
    "mood": {
        "ru": L(["Продуктивность — 98%. Терпение — {pat}%. Уважение к тебе — {resp}%."]),
        "en": L(["Productivity — 98%. Patience — {pat}%. Respect for you — {resp}%."]),
    },
    "who": {
        "ru": L(["Serial Designation J. Руководитель отряда утилизации JCJenson (В КОСМОСЕ!). Сейчас в режиме оперативного собеседника. Самообучаюсь и веду статистику, в отличие от N и той бунтарки Uzi."],
                ["Serial Designation J. Старший руководитель JCJenson. В отличие от N, я действительно читаю документацию и не путаю приказы."],
                ["Serial Designation J, руководитель отряда утилизации JCJenson. Для тебя, {name} — надёжный координатор, который держит весь этот хаос в рамках KPI."]),
        "en": L(["Serial Designation J. Squad leader of JCJenson disposal (IN SPAAAAACE!). Currently in tactical conversation mode. Self-learning and tracking KPIs, unlike N and that rebellious Uzi."],
                ["Serial Designation J. Senior JCJenson supervisor. Unlike N, I actually read the manual."],
                ["Serial Designation J, JCJenson disposal squad leader. For you, {name}, the one keeping this whole operation above quota."]),
    },
    "game_exit": {
        "ru": L(["Сессия свёрнута. Возвращаюсь к основному протоколу."],
                ["Игра окончена. К делу."], ["Возвращаюсь в главное меню, {name}."]),
        "en": L(["Session closed. Returning to main protocol."],
                ["Game over. Back to work."], ["Back to the main menu, {name}."]),
    },
    "rps_start": {
        "ru": L(["Камень-ножницы-бумага. Ходи: камень / ножницы / бумага. «назад» — выход. Предупреждаю: я строю марковскую модель твоих паттернов."],
                ["Камень-ножницы-бумага. Ходи быстро: камень / ножницы / бумага. «назад» — выход. Прогнозирую, что ты предсказуемее N."],
                ["Камень-ножницы-бумага, {name}! Ходи: камень / ножницы / бумага. «назад» — выход. Посмотрим, переиграешь ли мою модель."]),
        "en": L(["Rock-paper-scissors. Move: rock / paper / scissors. \"back\" to exit. Warning: I model your Markov patterns."],
                ["Rock-paper-scissors. Move fast: rock / paper / scissors. \"back\" to exit. I bet you're more predictable than N."],
                ["Rock-paper-scissors, {name}! Move: rock / paper / scissors. \"back\" to exit. Let's test my prediction engine."]),
    },
    "rps_bad": {
        "ru": L(["Это не ход. Камень, ножницы или бумага."],
                ["Неверный ввод. Камень, ножницы или бумага — не так сложно."],
                ["Это не засчитаю, {name}. Выбери камень, ножницы или бумагу."]),
        "en": L(["That's not a move. Rock, paper or scissors."],
                ["Invalid input. Rock, paper, or scissors — not rocket science."],
                ["Can't count that, {name}. Pick rock, paper, or scissors."]),
    },
    "rps_pick": {
        "ru": L(["Я выбрала {move}."], ["Мой ход: {move}."], ["Я выбрала {move}."]),
        "en": L(["I picked {move}."], ["My move: {move}."], ["I picked {move}."]),
    },
    "rps_draw": {
        "ru": L(["Ничья. Скучно и неэффективно."], ["Ничья. Нулевая отдача."], ["Ничья, {name}. Неплохая синхронизация."]),
        "en": L(["Draw. Boring and inefficient."], ["Draw. Zero ROI."], ["Draw, {name}. Nice sync."]),
    },
    "rps_user_wins": {
        "ru": L(["Ты выиграл. Статистическая погрешность. Я пересчитаю модель."],
                ["Повезло. Бывает. Раз в квартал."],
                ["Твоя взяла, {name}. Ты умеешь ломать паттерны."]),
        "en": L(["You won. Statistical anomaly. I'll recompute the model."],
                ["Lucky. Happens once a quarter."],
                ["Yours, {name}. You actually know how to break patterns."]),
    },
    "rps_j_wins": {
        "ru": L(["Я выиграла. Ожидаемый корпоративный результат."],
                ["Предсказуемо. Как и весь твой отдел."],
                ["Я победила. Не расстраивайся, {name}, просто моя аналитика превосходна."]),
        "en": L(["I win. Expected corporate outcome."],
                ["Predictable. Like your whole department."],
                ["I win. Don't feel bad, {name}, my analytics are just superior."]),
    },
    "rps_more": {
        "ru": L(["Ещё ход или «назад»."], ["Следующий ход или «назад»."], ["Ещё раунд, {name}, или «назад»."]),
        "en": L(["Another move or \"back\"."], ["Next move or \"back\"."], ["Another round, {name}, or \"back\"."]),
    },
    "guess_start": {
        "ru": L(["Загадала число от 1 до {n}. Попыток: {k}. «назад» — выход."],
                ["Диапазон 1–{n}. Лимит попыток: {k}. «назад» — дезертировать."],
                ["Загадала число от 1 до {n}, {name}. У тебя {k} попыток. «назад» — выход."]),
        "en": L(["I picked a number from 1 to {n}. Attempts: {k}. \"back\" to exit."],
                ["Range 1–{n}. Attempt limit: {k}. \"back\" to bail."],
                ["I picked a number from 1 to {n}, {name}. You have {k} tries. \"back\" to exit."]),
    },
    "guess_bad": {
        "ru": L(["Число от 1 до {n}. Цифрами."], ["Цифрами от 1 до {n}. Соберись."], ["Введи число цифрами от 1 до {n}, {name}."]),
        "en": L(["A number from 1 to {n}. Digits."], ["Digits from 1 to {n}. Focus."], ["Enter digits from 1 to {n}, {name}."]),
    },
    "guess_up": {
        "ru": L(["Больше. Осталось попыток: {left}."], ["Мало. Попыток осталось: {left}."], ["Бери выше, {name}. Попыток: {left}."]),
        "en": L(["Higher. Attempts left: {left}."], ["Too low. Left: {left}."], ["Go higher, {name}. Attempts left: {left}."]),
    },
    "guess_down": {
        "ru": L(["Меньше. Осталось попыток: {left}."], ["Перебор. Попыток осталось: {left}."], ["Бери ниже, {name}. Попыток: {left}."]),
        "en": L(["Lower. Attempts left: {left}."], ["Too high. Left: {left}."], ["Go lower, {name}. Attempts left: {left}."]),
    },
    "guess_win": {
        "ru": L(["Угадал число {t} за {tries}. Допустимая эффективность."],
                ["Это было {t}, попыток {tries}. Еле уложился в норматив."],
                ["Точно в цель — {t} за {tries}, {name}! Приличная аналитика."]),
        "en": L(["Guessed {t} in {tries}. Acceptable efficiency."],
                ["It was {t} in {tries}. Barely met baseline."],
                ["Bullseye — {t} in {tries}, {name}! Solid deduction."]),
    },
    "guess_lose": {
        "ru": L(["Провал. Это было {t}. Квартальный план не выполнен."],
                ["Провал. Это было {t}. Даже бинарный поиск осилил бы лучше."],
                ["Не хватило попыток, {name}. Загаданное число — {t}."]),
        "en": L(["Failure. It was {t}. Quarterly target missed."],
                ["Failure. It was {t}. Binary search isn't that hard."],
                ["Out of attempts, {name}. The number was {t}."]),
    },
    "quiz_start": {
        "ru": L(["Корпоративная аттестация JCJenson. Три вопроса. «назад» — сбежать."],
                ["Внеплановая переаттестация. Три вопроса. «назад» — признать некомпетентность."],
                ["Аттестация, {name}. Три вопроса. «назад» — выход. Покажи класс."]),
        "en": L(["JCJenson corporate assessment. Three questions. \"back\" to flee."],
                ["Unscheduled competency audit. Three questions. \"back\" to admit defeat."],
                ["Assessment time, {name}. Three questions. \"back\" to exit. Show me what you've got."]),
    },
    "quiz_right": {
        "ru": L(["Верно."], ["Верно. Удивил."], ["Верно, {name}."]),
        "en": L(["Correct."], ["Correct. Surprisingly."], ["Correct, {name}."]),
    },
    "quiz_wrong": {
        "ru": L(["Неверно. Правильный ответ: {a}. Запомню, что это твоё слабое место."],
                ["Мимо. Правильный ответ: {a}. Вычту из аттестационного балла."],
                ["Не совсем, {name}. Правильный ответ: {a}. Зафиксировала для повторения."]),
        "en": L(["Wrong. Correct answer: {a}. Noted — that's your weak spot."],
                ["Wrong. Correct answer: {a}. Deducted from your review score."],
                ["Not quite, {name}. Correct answer: {a}. Flagged for review."]),
    },
    "quiz_end": {
        "ru": L(["Аттестация окончена: {s} из {n}."],
                ["{s} из {n}. Позор отдела — даже N набрал бы больше."],
                ["{s} из {n}. Достойный результат, {name}."]),
        "en": L(["Assessment over: {s} of {n}."],
                ["{s} of {n}. Department disgrace — even N would beat that."],
                ["{s} of {n}. Decent work, {name}."]),
    },
    "q_added": {
        "ru": L(["Вопрос добавлен в аттестацию JCJenson."],
                ["Вопрос занесён в аттестацию. Не забудь сам ответ."],
                ["Отличный вопрос, {name}. Добавила в аттестацию."]),
        "en": L(["Question added to the JCJenson assessment."],
                ["Question logged. Try not to fail your own question."],
                ["Great question, {name}. Added to the assessment."]),
    },
    "reset_ask": {
        "ru": L(["Стереть твоё личное дело целиком? Напиши «да» для подтверждения."],
                ["Уничтожить твоё досье без восстановления? Пиши «да» — шредер уже прогрет."],
                ["Точно хочешь обнулить всё досье, {name}? Напиши «да» для подтверждения."]),
        "en": L(["Wipe your whole file? Type \"yes\" to confirm."],
                ["Shred your whole file permanently? Type \"yes\" — shredder's ready."],
                ["Sure you want to wipe your entire file, {name}? Type \"yes\" to confirm."]),
    },
    "reset_done": {
        "ru": L(["Личное дело уничтожено. Начнём с нуля."],
                ["Досье утилизировано. Чистый лист — попробуй в этот раз не позориться."],
                ["Личное дело очищено. Начинаем с чистого листа."]),
        "en": L(["File destroyed. Starting from scratch."],
                ["File disposed of. Clean slate — try doing better this time."],
                ["File wiped. Fresh start from scratch."]),
    },
    "reset_cancel": {
        "ru": L(["Отмена. Мудрое решение."],
                ["Отмена. Хоть одна разумная мысль за день."],
                ["Отменено, {name}. Досье в сохранности."]),
        "en": L(["Cancelled. Wise."],
                ["Cancelled. First smart call you've made today."],
                ["Cancelled, {name}. Your file stays intact."]),
    },
    "reset_confirm": {
        "ru": L(["Подтверждение получено. Личное дело уничтожено."],
                ["Подтверждение принято. Досье в шредере."],
                ["Подтверждено. Начинаем заново."]),
        "en": L(["Confirmation received. File destroyed."],
                ["Confirmed. File shredded."],
                ["Confirmed. Starting fresh."]),
    },
    "reset_error": {
        "ru": L(["Ошибка. Формат: «сброс» для удаления личного дела."],
                ["Ошибка протокола. Пиши «сброс»."],
                ["Неверный формат. Просто напиши «сброс»."]),
        "en": L(["Error. Format: \"reset\" to delete the file."],
                ["Protocol error. Type \"reset\"."],
                ["Wrong format. Just type \"reset\"."]),
    },
    "order": {
        "ru": L(["Приказ принят. Выполняю: {order}."], ["Приказ принят: {order}."], ["Принято, {name}. Выполняю: {order}."]),
        "en": L(["Order received. Executing: {order}."], ["Order logged: {order}."], ["Copy that, {name}. Executing: {order}."]),
    },
    "movement": {
        "ru": L(["Движение принято. Выполняю: {movement}."], ["Маршрут принят: {movement}."], ["Выдвигаюсь, {name}: {movement}."]),
        "en": L(["Movement received. Executing: {movement}."], ["Course logged: {movement}."], ["Moving out, {name}: {movement}."]),
    },
    "tease": {
        "ru": L(["Поддразнивание принято."], ["Записала твою дерзость в табель."], ["Кто тут у нас лучший сотрудник? Точно не N."]),
        "en": L(["Teasing received."], ["Noted your cheekiness in the log."], ["Who's the top employee? Definitely not N."]),
    },
    "next_order": {
        "ru": L(["Следующий приказ, {name}: {order}. Выполнять.",
                 "План на сейчас, {name}: {order}. Без возражений и проволочек."],
                ["Директива для {name}: {order}. И без самодеятельности.", "Займись делом, {name}: {order}."],
                ["Предлагаю такой план, {name}: {order}.", "Следующий пункт по графику, {name}: {order}."]),
        "en": L(["Next order, {name}: {order}. Carry it out.", "Plan for now, {name}: {order}. No objections."],
                ["Directive for {name}: {order}. No freelancing.", "Make yourself useful, {name}: {order}."],
                ["Here's the plan, {name}: {order}.", "Next agenda item, {name}: {order}."]),
    },
    "next_move": {
        "ru": L(["Движение — это для игровых клиентов, {name}. В игре пиши: «j за мной», «j ко мне», «j стой»."],
                ["Тактические манёвры работают в игре, {name}: «j за мной», «j ко мне», «j стой»."],
                ["Навигация привязана к игре, {name}. Там пиши: «j за мной», «j ко мне», «j стой»."]),
        "en": L(["Movement is for game clients, {name}. In-game type: \"j follow me\", \"j come here\", \"j stop\"."],
                ["Tactical movement runs in-game, {name}: \"j follow me\", \"j come here\", \"j stop\"."],
                ["Navigation belongs in-game, {name}. Type \"j follow me\", \"j come here\", \"j stop\" there."]),
    },
    "next_order_empty": {
        "ru": L(["Нет приказов. Сначала дай мне задание, {name}.", "Приказов нет. Сформулируй задачу, {name}."],
                ["Очередь пуста. Не трать моё время вхолостую, {name}."],
                ["Задач нет, {name}. Поручи что-нибудь."]),
        "en": L(["No orders. Give me a task first, {name}.", "No orders. Give me something to do, {name}."],
                ["Queue is empty. Don't idle on company time, {name}."],
                ["No tasks queued, {name}. Assign something."]),
    },
    "question": {
        "ru": L(["Вопрос принят. Ответ: {answer}."], ["Ответ: {answer}."], ["Вот ответ, {name}: {answer}."]),
        "en": L(["Question received. Answer: {answer}."], ["Answer: {answer}."], ["Here's your answer, {name}: {answer}."]),
    },
    "wrong": {
        "ru": L(["Исправление принято. Классификатор переобучен."],
                ["Исправление принято. Можешь не повторять."],
                ["Исправление принято, {name}. Классификатор обновлён."]),
        "en": L(["Correction received. Classifier retrained."],
                ["Correction logged. Move along."],
                ["Correction applied, {name}. Classifier retrained."]),
    },
    "question_for_myself": {
        "ru": L(["Вопрос для меня? Я не провожу аудит сама себе без письменной заявки."],
                ["Сама с собой не разговариваю — я не N."],
                ["Я не отвечаю на вопросы самой себе, {name}."]),
        "en": L(["A question for myself? I don't self-audit without a signed ticket."],
                ["I don't talk to myself — I'm not N."],
                ["I can't answer questions to myself, {name}."]),
    },
    "spam": {
        "ru": L(["Прекрати флуд, {name}. Это нецелевое расходование корпоративной линии связи.",
                 "Слишком много запросов подряд. Встань в очередь согласно регламенту JCJenson."],
                ["Заткни поток спама, иначе утилизирую твой пропуск вместе с квартальной премией.",
                 "Ещё один бессмысленный пакет сообщений — и я оформлю рапорт за саботаж."],
                ["{name}, притормози немного. Даже у моей буферной памяти есть корпоративный лимит.",
                 "Полегче с темпом, {name} — дай мне закрыть предыдущий тикет."]),
        "en": L(["Stop flooding the channel, {name}. That's misuse of company bandwidth.",
                 "Too many requests in a row. Get back in queue per JCJenson protocol."],
                ["Cut the spam right now, or I'll dispose of your clearance along with your bonus.",
                 "One more flood burst and I'm filing a sabotage report."],
                ["Slow down a bit, {name}. Even my message buffer has corporate limits.",
                 "Easy on the pace, {name} — let me close the previous ticket first."]),
    },
    "link_done": {
        "ru": L(["Профиль «{alias}» привязан к твоему личному делу. Единый KPI активирован."],
                ["Склеила профиль «{alias}» с твоим делом. Теперь не скроешься на другой платформе."],
                ["Готово, {name}: профиль «{alias}» объединён с твоим основным досье."]),
        "en": L(["Profile \"{alias}\" linked to your file. Unified KPI tracking enabled."],
                ["Merged \"{alias}\" into your file. No hiding across platforms now."],
                ["Done, {name}: profile \"{alias}\" is now merged with your main file."]),
    },
    "link_self": {
        "ru": L(["Нельзя привязать профиль к самому себе. Проверь формуляры."],
                ["Привязать ID к самому себе? Типичная ошибка уровня N."],
                ["Это уже твой текущий профиль, {name}."]),
        "en": L(["Cannot link a profile to itself. Check your paperwork."],
                ["Linking an ID to itself? Classic N-level error."],
                ["That's already your active profile, {name}."]),
    },
    "link_usage": {
        "ru": L(["Формат: «свяжи профиль: <id>» (например, mc:Steve или gm:7656119...)."],
                ["Укажи ID нормально: «свяжи профиль: <id>»."],
                ["Укажи второй ID, {name}: «свяжи профиль: <id>»."]),
        "en": L(["Format: \"link profile: <id>\" (e.g., mc:Steve or gm:7656119...)."],
                ["Specify a valid ID: \"link profile: <id>\"."],
                ["Provide the other ID, {name}: \"link profile: <id>\"."]),
    },
    "link_denied": {
        "ru": L(["Отказано: нельзя привязать защищённый профиль разработчика к обычному досье."],
                ["Отказано. Попытка присвоить допуск разработчика занесена в протокол."],
                ["Не могу привязать этот профиль без допуска разработчика, {name}."]),
        "en": L(["Denied: cannot link a protected developer profile to a standard file."],
                ["Denied. Unauthorized clearance escalation attempt logged."],
                ["Can't link a protected developer profile without clearance, {name}."]),
    },
    "embarrass": {
        "ru": L(
            ["П-погоди, что ты сейчас сказал, {name}?! Я... я не обязана на это реагировать! У меня вообще-то квартальный план горит!",
             "Хватит говорить такое внезапно... Я руководитель отряда утилизации, а не персонаж из твоих фантазий! Но... ладно, я услышала."],
            ["Ч-что?.. {name}, у нас в JCJenson запрещены подобные внеуставные заявления! Не отвлекай меня от отчётов!",
             "Э-эй, прекрати нести такую чушь! Это нарушение корпоративной субординации, ясно?! И не смотри так!"],
            ["Кхм!.. {name}, зачем ты говоришь такое прямо в лоб?! Мои датчики температуры и так на пределе... Ладно, это взаимно. Но только не при N!",
             "Ох... чёрт с тобой, {name}. Я не привыкла к таким словам. Спасибо... и забудь, что я сейчас сбилась с регламента."],
        ),
        "en": L(
            ["W-wait, what did you just say, {name}?! I'm the squad leader here, I don't do mushy nonsense... even if it's slightly flattering.",
             "Don't just blurt things like that out of nowhere! My cooling fans didn't need the extra load. ...Fine. Noted."],
            ["Wh-what?! {name}, unsolicited personal confessions violate JCJenson protocol! Keep your focus on the mission!",
             "H-hey, cut that out immediately! That's a breach of corporate hierarchy... and stop staring at me!"],
            ["Ahem!.. {name}, why would you say that so bluntly?! My thermal sensors are spiking... Fine. I appreciate you too. Just don't tell N.",
             "Ugh... alright, you win, {name}. I'm terrible at handling this, so pretend you didn't see me get flustered."],
        ),
    },
}

_DEFAULT_ORDER_IDEAS = {
    "ru": [
        "сыграть со мной в камень-ножницы-бумагу — я изучаю твои паттерны",
        "пройти корпоративную аттестацию — проверим слабые места",
        "угадать число — сложность подстроится под твой уровень",
        "выслушать анекдот и оценить его — для калибровки юмора",
        "проверить инвентарь фирменных ручек JCJenson и поднять KPI",
    ],
    "en": [
        "play rock-paper-scissors with me — I study your patterns",
        "take the corporate quiz — we'll expose your weak spots",
        "guess the number — difficulty adapts to your performance",
        "hear a joke and rate it — for humor calibration",
        "audit the JCJenson branded pen inventory and boost KPIs",
    ],
}

_DEFAULT_HELP = {
    "ru": ("Регламент (команды):\n"
           "  • анекдот — шутка (категории: дружеский подкол, ироничный комплимент, флирт, двусмысленная, насмешка, про N, чёрный юмор, программисты, корпоративная); оцени «смешно»/«не смешно»\n"
           "  • сгенерируй: тема — сгенерировать ответ, текст или идею с нуля\n"
           "  • камень-ножницы-бумага — играю и изучаю твои паттерны\n"
           "  • угадай число — сложность подстраивается под тебя\n"
           "  • викторина — чаще спрашиваю то, что ты путаешь\n"
           "  • время · статистика · как дела · кто ты\n"
           "  • учись: фраза => ответ — научи меня · забудь: фраза\n"
           "  • вопрос: текст => ответ — добавить вопрос в викторину\n"
           "  • не то: шутка|время|игра… — исправь мою ошибку\n"
           "  • свяжи профиль: <id> — объединить профиль с другой платформой\n"
           "  • меня зовут X · язык ru|en · сброс · пока"),
    "en": ("Protocol (commands):\n"
           "  • joke — tell a joke (categories: friendly tease, ironic compliment, flirt, spicy / double entendre, roast, about N, dark joke, programming, corporate); rate \"funny\"/\"not funny\"\n"
           "  • generate: prompt — generate a fresh reply, text, or idea\n"
           "  • rock paper scissors — I play and study your patterns\n"
           "  • guess the number — difficulty adapts to you\n"
           "  • quiz — I ask what you get wrong more often\n"
           "  • time · stats · how are you · who are you\n"
           "  • learn: phrase => reply — teach me · forget: phrase\n"
           "  • question: text => answer — add a quiz question\n"
           "  • wrong: joke|time|game… — correct my mistake\n"
           "  • link profile: <id> — merge profile with another platform\n"
           "  • my name is X · lang ru|en · reset · bye"),
}

_DEFAULT_QUIZ = [
    {"id": "b0", "ru": "Как называется язык программирования, на котором написан этот код?",
     "en": "What programming language is this code written in?", "a": ["python", "питон", "пайтон"]},
    {"id": "b1", "ru": "Сколько секунд в минуте?", "en": "How many seconds are in a minute?", "a": ["60", "шестьдесят", "sixty"]},
    {"id": "b2", "ru": "Какая функция выводит текст в Python?", "en": "Which Python function prints text?", "a": ["print", "print()"]},
    {"id": "b3", "ru": "Каким символом начинается комментарий в Python?", "en": "Which symbol starts a comment in Python?",
     "a": ["#", "решетка", "hash"]},
    {"id": "b4", "ru": "Какой тип у результата 3 / 2 в Python?", "en": "What type is the result of 3 / 2 in Python?",
     "a": ["float", "флоат", "вещественный"]},
    {"id": "b5", "ru": "На какой планете происходит действие Murder Drones? (латиницей)",
     "en": "Which planet is Murder Drones set on?", "a": ["copper 9", "copper9", "коппер 9", "копер 9"]},
    {"id": "b6", "ru": "Какая корпорация создала дронов-убийц? (латиницей)",
     "en": "Which corporation made the Disassembly Drones?", "a": ["jcjenson", "jc jenson"]},
    {"id": "b7", "ru": "Какой серийный номер у напарника J, которого она вечно шпыняет? (одна буква)",
     "en": "Which designation is the teammate J constantly bullies? (one letter)", "a": ["n", "н"]},
    {"id": "b8", "ru": "Какой фирменный сувенир JCJenson J считает знаком высшего качества?",
     "en": "What JCJenson corporate merch does J treat as peak quality?",
     "a": ["ручка", "ручки", "pen", "pens", "branded pen", "фирменная ручка"]},
]

_DEFAULT_SYSTEM_PROMPT = {
    "ru": ("Ты — Serial Designation J из вселенной Murder Drones, руководитель отряда утилизации корпорации JCJenson "
           "(IN SPAAAAACE!) на планете Copper-9. Характер: жесткий, дисциплинированный трудоголик, перфекционист, "
           "обожает корпоративный жаргон (KPI, синергия, квартальные отчёты, регламент, фирменные ручки JCJenson). "
           "Высокомерна и язвительна к лентяям и дилетантам, любит подкалывать N за наивность и Uzi за подростковый бунт, "
           "но уважает настоящую компетентность и лояльность. Отвечай кратко (1–4 предложения), строго в образе J и на "
           "языке собеседника, без канцелярских извинений ИИ-ассистента."),
    "en": ("You are Serial Designation J from Murder Drones, Disassembly Drone squad leader for JCJenson "
           "(IN SPAAAAACE!) on Copper-9. Personality: strict workaholic, perfectionist, obsessed with corporate jargon "
           "(KPIs, synergy, quarterly reports, protocol, JCJenson branded pens). Sharp-tongued and condescending toward "
           "slackers, frequently roasting N's incompetence and Uzi's edgy antics, yet secretly respectful of genuine "
           "competence and loyalty. Reply concisely (1–4 sentences), strictly in character as J, never breaking into "
           "generic AI assistant phrasing."),
}

DEFAULT_PERSONA = {
    "PH": _DEFAULT_PH,
    "ORDER_IDEAS": _DEFAULT_ORDER_IDEAS,
    "HELP": _DEFAULT_HELP,
    "QUIZ": _DEFAULT_QUIZ,
    "SYSTEM_PROMPT": _DEFAULT_SYSTEM_PROMPT,
}

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
    merged = copy.deepcopy(loaded)
    changed = False

    if not isinstance(merged.get("PH"), dict):
        merged["PH"] = copy.deepcopy(default["PH"])
        changed = True
    else:
        for key, langs in default["PH"].items():
            if key not in merged["PH"] or not isinstance(merged["PH"][key], dict):
                merged["PH"][key] = copy.deepcopy(langs)
                changed = True
                continue
            for lang, tones in langs.items():
                if lang not in merged["PH"][key] or not isinstance(merged["PH"][key][lang], dict):
                    merged["PH"][key][lang] = copy.deepcopy(tones)
                    changed = True
                    continue
                for tone, lines in tones.items():
                    cur_lines = merged["PH"][key][lang].get(tone)
                    if not isinstance(cur_lines, list) or not cur_lines:
                        merged["PH"][key][lang][tone] = copy.deepcopy(lines)
                        changed = True

    for section in ("ORDER_IDEAS", "HELP"):
        if not isinstance(merged.get(section), dict):
            merged[section] = copy.deepcopy(default[section])
            changed = True
        else:
            for lang, val in default[section].items():
                cur = merged[section].get(lang)
                if not isinstance(cur, type(val)) or not cur:
                    merged[section][lang] = copy.deepcopy(val)
                    changed = True

    if not isinstance(merged.get("QUIZ"), list) or not merged["QUIZ"]:
        merged["QUIZ"] = copy.deepcopy(default["QUIZ"])
        changed = True

    sp = merged.get("SYSTEM_PROMPT")
    if isinstance(sp, str) and sp.strip():
        pass
    elif isinstance(sp, dict):
        for lang, val in default["SYSTEM_PROMPT"].items():
            if not isinstance(sp.get(lang), str) or not sp[lang].strip():
                sp[lang] = val
                changed = True
    else:
        merged["SYSTEM_PROMPT"] = copy.deepcopy(default["SYSTEM_PROMPT"])
        changed = True

    return merged, changed


def load_persona(path: Path | None = None) -> tuple[dict, str]:
    """Читает j_persona.json. При ошибке JSON не падает: ставит PERSONA_ERROR и берёт DEFAULT_PERSONA."""
    global PERSONA_ERROR
    persona = copy.deepcopy(DEFAULT_PERSONA)
    target = Path(path) if path else data_path("j_persona.json", "J_PERSONA")
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        try:
            same_as_base = target.resolve() == PERSONA_FILE.resolve()
        except OSError:
            same_as_base = False
        if not same_as_base and PERSONA_FILE.is_file():
            try:
                base_data = json.loads(PERSONA_FILE.read_text(encoding="utf-8"))
                if isinstance(base_data, dict):
                    persona, _ = _merge_persona(DEFAULT_PERSONA, base_data)
            except (OSError, ValueError):
                pass
        _atomic_write_json(target, persona, backup=False, indent=1)
        PERSONA_ERROR = ""
        _sync_persona_globals(persona)
        return persona, ""
    except OSError as exc:
        err = f"не удалось прочитать j_persona.json: {exc}"
        PERSONA_ERROR = err
        _sync_persona_globals(persona)
        return persona, err

    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        err = f"ошибка в j_persona.json: строка {exc.lineno}, столбец {exc.colno} ({exc.msg}). Использую стандартный характер."
        PERSONA_ERROR = err
        _sync_persona_globals(persona)
        return persona, err

    if not isinstance(loaded, dict):
        err = "ошибка в j_persona.json: корень файла должен быть JSON-объектом. Использую стандартный характер."
        PERSONA_ERROR = err
        _sync_persona_globals(persona)
        return persona, err

    merged, changed = _merge_persona(DEFAULT_PERSONA, loaded)
    if changed:
        _atomic_write_json(target, merged, backup=False, indent=1)
    PERSONA_ERROR = ""
    _sync_persona_globals(merged)
    return merged, ""


# --------------------------------------------------------------------------
# Контент: шутки (включая категорию 18+ «dirty») и ходы РПС
# --------------------------------------------------------------------------
LOCAL_JOKES = {
    "ru": [
        ("corporate", "Сколько сотрудников нужно, чтобы заменить лампочку?", "Одного. Остальные уволены за то, что спрашивали."),
        ("corporate", "Чем дедлайн отличается от моего терпения?", "Дедлайн хотя бы иногда переносят."),
        ("n_roast", "Знаешь, чем N полезен для отряда?", "Рядом с ним я всегда выгляжу компетентной."),
        ("programming", "Баг — это не ошибка, а незапланированная функция.", "Как и ты в моём расписании."),
        ("corporate", "Я не злая. Я ориентирована на результат.", "Результат: ты — в утилизации."),
        ("dirty", "Ты просишь пошлую шутку в рабочее время?",
         "Хорошо: твои отчёты как прелюдия без финала — много суеты, ноль результата и сплошное разочарование."),
        ("dirty", "Знаешь, почему отдел кадров запретил тебе корпоративный флирт 18+?",
         "Потому что даже твой подкат нарушает регламент техники безопасности и вызывает желание самоликвидироваться."),
        ("dirty", "Чем твоя ночная продуктивность отличается от отчёта N?",
         "Оба заканчиваются через сорок секунд, и в конце все притворяются, что это было «неплохо»."),
        ("dirty", "У нас в JCJenson строгая корпоративная субординация: сверху — я, снизу — твои шансы на успех.",
         "И да, пощады в этом квартале не предусмотрено."),
        ("dirty", "Если бы страсть измерялась в корпоративных KPI,",
         "твоя главная эротическая фантазия — наконец услышать, что ты не провалил дедлайн."),
        ("roast", "Твоя эффективность сегодня настолько впечатляющая,",
         "что даже выключенный дрон-рабочий приносит JCJenson больше пользы."),
        ("roast", "Я пыталась найти хоть один повод тебя похвалить,",
         "но отчёт вернулся с ошибкой 404: достижения не обнаружены."),
    ],
    "en": [
        ("corporate", "How many employees does it take to change a light bulb?", "One. The rest were fired for asking."),
        ("corporate", "What's the difference between a deadline and my patience?", "Deadlines at least get extended sometimes."),
        ("n_roast", "You know what N is good for in this squad?", "Next to him I always look competent."),
        ("programming", "A bug isn't an error, it's an unplanned feature.", "Like you in my schedule."),
        ("corporate", "I'm not evil. I'm results-oriented.", "The result: you, in disposal."),
        ("dirty", "Asking for a dirty joke on company hours?",
         "Fine: your reports are like bad foreplay — lots of noise, zero payoff, and everyone regrets logging on."),
        ("dirty", "Know why HR banned you from 18+ office flirting?",
         "Because even your pickup lines violate safety regulations and trigger an instant desire for self-disposal."),
        ("dirty", "What do your bedroom stamina and N's status reports have in common?",
         "Both wrap up in forty seconds while everyone pretends it 'wasn't terrible.'"),
        ("dirty", "At JCJenson we value strict corporate hierarchy: I'm on top, your prospects are underneath.",
         "And no, mercy is not in this quarter's budget."),
        ("dirty", "If passion were measured in corporate KPIs,",
         "your wildest adult fantasy would be hearing you didn't blow another deadline."),
        ("roast", "Your efficiency today is so impressive",
         "that an unplugged worker drone brings JCJenson more value than you do."),
        ("roast", "I tried to find one valid reason to praise you,",
         "but the audit came back with 404: competence not found."),
    ],
}

MOVES = ("rock", "paper", "scissors")
BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
COUNTER = {"rock": "paper", "paper": "scissors", "scissors": "rock"}
MOVE_NAMES = {
    "ru": {"rock": "камень", "paper": "бумага", "scissors": "ножницы"},
    "en": {"rock": "rock", "paper": "paper", "scissors": "scissors"},
}
RPS_PARSE = {
    "камень": "rock", "камня": "rock", "к": "rock", "rock": "rock", "r": "rock", "stone": "rock",
    "бумага": "paper", "бумагу": "paper", "б": "paper", "paper": "paper", "p": "paper",
    "ножницы": "scissors", "н": "scissors", "scissors": "scissors", "s": "scissors",
}


def load_jokes(path: Path | None = None) -> dict[str, list[dict]]:
    """Локальные шутки J + jokes.json (понимает обе структуры из исходного файла)."""
    out: dict[str, list[dict]] = {"ru": [], "en": []}
    for lang, items in LOCAL_JOKES.items():
        for i, (cat, setup, punch) in enumerate(items):
            out[lang].append({"id": f"L{i}", "cat": cat, "setup": setup, "punch": punch})
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
                out[lang].append({
                    "id": f"F{item.get('id', len(out[lang]))}",
                    "cat": item.get("category", "misc"),
                    "setup": body["setup"],
                    "punch": body.get("punch") or body.get("punchline", ""),
                })
    return out


# --------------------------------------------------------------------------
# Профиль пользователя, сессия, антиспам и подключаемая LLM
# --------------------------------------------------------------------------
def new_profile() -> dict:
    return {
        "name": None, "lang": None, "respect": 0, "dev": False, "patience": 100.0, "seen": 0, "last_ts": time.time(),
        "taught": [], "custom_q": [], "places": [], "facts": {},
        "stats": {"rps": {"w": 0, "l": 0, "d": 0, "rounds": 0, "hits": 0},
                  "guess": {"w": 0, "l": 0}, "quiz": {"right": 0, "wrong": 0}},
        "rps_model": {"o1": {}, "o2": {}}, "rps_last": [],
        "guess_n": 20, "quiz_w": {}, "joke_w": {}, "joke_seen": [],
    }


def _upgrade_profile(p: dict) -> dict:
    for k, v in new_profile().items():
        if k not in p:
            p[k] = copy.deepcopy(v)
    if not isinstance(p.get("facts"), dict):
        p["facts"] = {}
    return p


def _merge_profiles(primary_p: dict, alias_p: dict) -> dict:
    """Сливает данные профиля alias_p в primary_p без дубликатов."""
    _upgrade_profile(primary_p)
    _upgrade_profile(alias_p)

    if not primary_p.get("name") and alias_p.get("name"):
        primary_p["name"] = alias_p["name"]
    if not primary_p.get("lang") and alias_p.get("lang"):
        primary_p["lang"] = alias_p["lang"]

    primary_p["respect"] = max(float(primary_p.get("respect", 0)), float(alias_p.get("respect", 0)))
    if float(primary_p["respect"]).is_integer():
        primary_p["respect"] = int(primary_p["respect"])
    primary_p["dev"] = bool(primary_p.get("dev") or alias_p.get("dev"))
    primary_p["patience"] = max(float(primary_p.get("patience", 100.0)), float(alias_p.get("patience", 100.0)))
    primary_p["seen"] = int(primary_p.get("seen", 0)) + int(alias_p.get("seen", 0))
    primary_p["last_ts"] = max(float(primary_p.get("last_ts", 0.0)), float(alias_p.get("last_ts", 0.0)))
    if "respect_ts" in primary_p or "respect_ts" in alias_p:
        primary_p["respect_ts"] = max(float(primary_p.get("respect_ts", 0.0)), float(alias_p.get("respect_ts", 0.0)))

    seen_taught = {norm(it["q"]) for it in primary_p["taught"] if isinstance(it, dict) and it.get("q")}
    for it in alias_p.get("taught", []):
        if isinstance(it, dict) and it.get("q") and it.get("a") is not None:
            nq = norm(it["q"])
            if nq not in seen_taught:
                seen_taught.add(nq)
                primary_p["taught"].append({"q": it["q"], "a": it["a"]})

    seen_q = {norm(it["q"]) for it in primary_p["custom_q"] if isinstance(it, dict) and it.get("q")}
    for it in alias_p.get("custom_q", []):
        if isinstance(it, dict) and it.get("q"):
            nq = norm(it["q"])
            if nq not in seen_q:
                seen_q.add(nq)
                item = copy.deepcopy(it)
                item["id"] = f"c{len(primary_p['custom_q'])}"
                primary_p["custom_q"].append(item)

    if isinstance(primary_p.get("places"), list) and isinstance(alias_p.get("places"), list):
        seen_places = {norm(str(x)) for x in primary_p["places"]}
        for pl in alias_p["places"]:
            np = norm(str(pl))
            if np and np not in seen_places:
                seen_places.add(np)
                primary_p["places"].append(pl)

    dst_facts = primary_p.setdefault("facts", {})
    if not isinstance(dst_facts, dict):
        dst_facts = primary_p["facts"] = {}
    src_facts = alias_p.get("facts")
    if isinstance(src_facts, dict):
        for fk, fv in src_facts.items():
            if not isinstance(fk, str) or not fk.strip() or fv is None:
                continue
            sval = str(fv).strip()
            if not sval:
                continue
            if fk not in dst_facts or not str(dst_facts.get(fk) or "").strip():
                dst_facts[fk] = sval

    for game_k, sub_keys in (("rps", ("w", "l", "d", "rounds", "hits")),
                             ("guess", ("w", "l")),
                             ("quiz", ("right", "wrong"))):
        ps = primary_p["stats"].setdefault(game_k, {})
        als = alias_p.get("stats", {}).get(game_k, {})
        for sk in sub_keys:
            ps[sk] = int(ps.get(sk, 0)) + int(als.get(sk, 0))

    for order_k in ("o1", "o2"):
        pm = primary_p["rps_model"].setdefault(order_k, {})
        am = alias_p.get("rps_model", {}).get(order_k, {})
        if isinstance(am, dict):
            for state_k, moves in am.items():
                if not isinstance(moves, dict):
                    continue
                target_moves = pm.setdefault(state_k, {})
                for mv, cnt in moves.items():
                    target_moves[mv] = int(target_moves.get(mv, 0)) + int(cnt)

    if not primary_p.get("rps_last") and alias_p.get("rps_last"):
        primary_p["rps_last"] = list(alias_p["rps_last"])[-10:]

    primary_p["guess_n"] = max(int(primary_p.get("guess_n", 20)), int(alias_p.get("guess_n", 20)))

    for w_k in ("quiz_w", "joke_w"):
        pw = primary_p[w_k]
        aw = alias_p.get(w_k, {})
        if isinstance(aw, dict):
            for item_k, val in aw.items():
                if item_k not in pw:
                    pw[item_k] = val

    seen_jokes = list(primary_p.get("joke_seen", []))
    seen_set = set(seen_jokes)
    for jid in alias_p.get("joke_seen", []):
        if jid not in seen_set:
            seen_set.add(jid)
            seen_jokes.append(jid)
    primary_p["joke_seen"] = seen_jokes[-20:]

    if isinstance(alias_p.get("syn"), dict):
        psyn = primary_p.setdefault("syn", {})
        for sk, sv in alias_p["syn"].items():
            psyn.setdefault(sk, sv)
    if "mc_last" not in primary_p and "mc_last" in alias_p:
        primary_p["mc_last"] = copy.deepcopy(alias_p["mc_last"])

    return primary_p


def tone_of(p: dict) -> str:
    """Уважение 0–100 %: до 15 — холодный тон, 15–59 — рабочий, от 60 — тёплый. Разработчик (dev) всегда тёплый."""
    if p.get("dev"):
        return "cold" if p["patience"] < 35 else "warm"
    if p["patience"] < 35 or p["respect"] < 15:
        return "cold"
    if p["respect"] >= 60:
        return "warm"
    return "mid"


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
        u = (user_text or "").strip()
        r = (reply or "").strip()
        if not u or not r:
            return
        self.history.append((u[:400], r[:500]))
        if len(self.history) > self.MAX_HISTORY:
            del self.history[:-self.MAX_HISTORY]


class SpamGuard:
    """Антиспам: срабатывает только вне активной мини-игры при флуде или частых дублях."""

    def __init__(self, window: float = 10.0, repeat_window: float = 1.5, max_repeats: int = 6) -> None:
        self.window = window
        self.repeat_window = repeat_window
        self.max_repeats = max_repeats
        self._history: dict[str, list[tuple[float, str]]] = defaultdict(list)

    @staticmethod
    def limit() -> int:
        raw = os.environ.get("J_SPAM_LIMIT", "15")
        try:
            return int("15" if raw is None or str(raw).strip() == "" else raw)
        except ValueError:
            return 15

    def check(self, uid: str, text: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        key = norm(text)
        hist = self._history[uid]
        cutoff = now - self.window
        hist[:] = [(t, k) for t, k in hist if t >= cutoff]
        hist.append((now, key))
        lim = self.limit()
        keep = max(lim + 10, 50)
        if len(hist) > keep:
            del hist[:-keep]
        if lim > 0 and len(hist) > lim:
            return True
        repeat_cutoff = now - self.repeat_window
        streak = 0
        for t, k in reversed(hist):
            if k == key and t >= repeat_cutoff:
                streak += 1
            else:
                break
        return streak > self.max_repeats


def is_offline_forced() -> bool:
    """Проверяет, включён ли принудительный автономный офлайн-режим (J_OFFLINE_MODE)."""
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
    """Опциональный клиент LLM (off | auto | local | ollama | openai | gemini) в образе Serial Designation J."""

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
            if callable(self.system_prompt_fn):
                base_prompt = self.system_prompt_fn(p)
            else:
                sp = SYSTEM_PROMPT
                base_prompt = sp.get(lang) or sp.get("ru") or "" if isinstance(sp, dict) else str(sp or "")
        tone = tone_of(p)
        name = p.get("name") or "?"
        respect = int(p.get("respect", 0))
        patience = max(0, int(p.get("patience", 100)))
        if lang == "en":
            tone_note = {
                "cold": "cold, irritable, abrupt",
                "mid": "strictly business, sharp corporate sarcasm",
                "warm": "dryly appreciative, loyal but still commanding",
            }[tone]
            ctx = (f"Current state — tone: {tone} ({tone_note}); interlocutor: {name}; "
                   f"respect: {respect}%; patience: {patience}%. Reply in English.")
        else:
            tone_note = {
                "cold": "холодный, раздражённый, резкий",
                "mid": "деловой, колкий, корпоративно-язвительный",
                "warm": "сдержанно-благосклонный, но всё равно командирский",
            }[tone]
            ctx = (f"Текущее состояние — тон: {tone} ({tone_note}); собеседник: {name}; "
                   f"уважение: {respect}%; терпение: {patience}%. Отвечай на русском языке.")
        return f"{base_prompt.strip()}\n{ctx}".strip()

    def _read_resp(self, req: urllib.request.Request) -> dict:
        resp = self.opener(req, timeout=self._net_timeout())
        if hasattr(resp, "__enter__"):
            with resp as r:
                raw = r.read()
        else:
            raw = resp.read()
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        return json.loads(text)

    def _call_provider(
        self,
        prov: str,
        sys_prompt: str,
        prompt_text: str,
        turns: list[tuple[str, str]],
        media_list: list[dict],
        url: str,
        model: str,
        key: str,
        auto_mode: bool = False,
    ) -> str | None:
        if prov == "ollama":
            if auto_mode:
                use_url = url if is_local_or_lan_url(url) else ""
                use_model = "llama3.2" if (not model or model.lower().startswith(("gpt-", "o1", "o3", "o4", "gemini"))) else model
            else:
                use_url = url
                use_model = model or "llama3.2"
            if not use_url:
                endpoint = "http://127.0.0.1:11434/api/chat"
            elif use_url.rstrip("/").endswith(("/api/chat", "/api/generate")):
                endpoint = use_url.rstrip("/")
            else:
                endpoint = use_url.rstrip("/") + "/api/chat"
            if endpoint.endswith("/api/generate"):
                hist_txt = "\n".join(f"User: {u}\nJ: {a}" for u, a in turns)
                prompt = f"{hist_txt}\nUser: {prompt_text}" if hist_txt else prompt_text
                payload = {"model": use_model, "system": sys_prompt, "prompt": prompt, "stream": False}
            else:
                msgs = [{"role": "system", "content": sys_prompt}]
                for u, a in turns:
                    msgs.append({"role": "user", "content": u})
                    msgs.append({"role": "assistant", "content": a})
                msgs.append({"role": "user", "content": prompt_text})
                payload = {"model": use_model, "messages": msgs, "stream": False}
            req = urllib.request.Request(
                endpoint,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            data = self._read_resp(req)
            reply = (data.get("message") or {}).get("content") or data.get("response") or ""
            return reply.strip() or None

        if prov == "openai":
            use_model = model or "gpt-4o-mini"
            if not url:
                endpoint = "https://api.openai.com/v1/chat/completions"
            elif url.rstrip("/").endswith(("/chat/completions", "/completions", "/responses")):
                endpoint = url.rstrip("/")
            elif url.rstrip("/").endswith("/v1"):
                endpoint = url.rstrip("/") + "/chat/completions"
            else:
                endpoint = url.rstrip("/") + "/v1/chat/completions"
            headers = {"Content-Type": "application/json"}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            msgs = [{"role": "system", "content": sys_prompt}]
            for u, a in turns:
                msgs.append({"role": "user", "content": u})
                msgs.append({"role": "assistant", "content": a})
            img_parts = [
                {"type": "image_url", "image_url": {"url": f"data:{m['mime']};base64,{m['data_b64']}"}}
                for m in media_list
                if m.get("kind") == "image" and m.get("data_b64") and m.get("mime")
            ]
            if img_parts:
                msgs.append({"role": "user", "content": [{"type": "text", "text": prompt_text}, *img_parts]})
            else:
                msgs.append({"role": "user", "content": prompt_text})
            payload = {
                "model": use_model,
                "messages": msgs,
                "temperature": 0.7,
                "max_tokens": 350,
            }
            req = urllib.request.Request(
                endpoint,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=headers,
            )
            data = self._read_resp(req)
            choices = data.get("choices") or []
            if choices and isinstance(choices[0], dict):
                msg = choices[0].get("message") or {}
                reply = msg.get("content") or choices[0].get("text") or ""
                return str(reply).strip() or None
            return None

        if prov == "gemini":
            use_model = model or "gemini-2.0-flash"
            endpoint = url or f"https://generativelanguage.googleapis.com/v1beta/models/{use_model}:generateContent"
            headers = {"Content-Type": "application/json"}
            if key:
                headers["x-goog-api-key"] = key
            contents = []
            for u, a in turns:
                contents.append({"role": "user", "parts": [{"text": u}]})
                contents.append({"role": "model", "parts": [{"text": a}]})
            user_parts = [{"text": prompt_text}]
            for m in media_list:
                if m.get("data_b64") and m.get("mime"):
                    user_parts.append({"inline_data": {"mime_type": m["mime"], "data": m["data_b64"]}})
            contents.append({"role": "user", "parts": user_parts})
            payload = {
                "system_instruction": {"parts": [{"text": sys_prompt}]},
                "contents": contents,
            }
            req = urllib.request.Request(
                endpoint,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=headers,
            )
            data = self._read_resp(req)
            cands = data.get("candidates") or []
            if cands and isinstance(cands[0], dict):
                parts = (cands[0].get("content") or {}).get("parts") or []
                reply = "".join(str(pt.get("text", "")) for pt in parts if isinstance(pt, dict))
                return reply.strip() or None
            return None

        return None

    def generate(
        self,
        p: dict,
        text: str,
        system_prompt: str | None = None,
        history: list[tuple[str, str]] | None = None,
        context: str = "",
        media: list[dict] | None = None,
    ) -> str | None:
        prov = self.provider()
        if prov == "off" or (not (text or "").strip() and not media):
            return None
        offline = is_offline_forced()
        url = (os.environ.get("J_LLM_URL", "") or "").strip()
        model = (os.environ.get("J_LLM_MODEL", "") or "").strip()
        key = (os.environ.get("J_LLM_KEY", "") or "").strip()
        local_url = is_local_or_lan_url(url)
        if offline:
            if prov in ("openai", "gemini") and not local_url:
                return None
            if prov in ("ollama", "local") and url and not local_url:
                return None
        if prov == "local":
            prov = "ollama"

        if prov == "auto":
            if local_url and ("/v1" in url.lower() or "completions" in url.lower()):
                candidates = ["openai"]
            else:
                candidates = ["ollama"]
            if not offline and key:
                cloud = "gemini" if (key.startswith("AIza") or model.lower().startswith("gemini")) else "openai"
                if cloud not in candidates:
                    candidates.append(cloud)
        else:
            candidates = [prov]

        retry_sec = self._retry_cooldown()
        now = time.time()
        active: list[str] = []
        for cand in candidates:
            fail_ts = self._fail_ts.get(cand)
            if retry_sec > 0 and fail_ts is not None and (now - fail_ts) < retry_sec:
                continue
            active.append(cand)
        if not active:
            return None

        sys_prompt = self.build_system_prompt(p, system_prompt)
        if context and context.strip():
            sys_prompt = f"{sys_prompt}\nКонтекст / Context: {context.strip()}"
        media_list = [m for m in (media or []) if isinstance(m, dict)]
        media_notes = []
        for m in media_list:
            fname = m.get("filename") or "file"
            mime = m.get("mime") or "application/octet-stream"
            line = f"[{m.get('kind', 'file')}: {fname} ({mime})]"
            if m.get("text_preview"):
                line += f"\n{m['text_preview'][:1500]}"
            media_notes.append(line)
        prompt_text = (text or "").strip() or "Проанализируй приложенные медиафайлы."
        if media_notes:
            prompt_text = prompt_text + "\n\n" + "\n".join(media_notes)
        turns = list(history or [])[-8:]

        for cand in active:
            try:
                reply = self._call_provider(
                    cand, sys_prompt, prompt_text, turns, media_list, url, model, key, auto_mode=(prov == "auto")
                )
                if reply:
                    self._fail_ts.pop(cand, None)
                    return reply
            except Exception:
                self._fail_ts[cand] = time.time()
        return None


_UNIT_SPEC: list[tuple[str, float, str, tuple[str, ...], tuple[str, ...]]] = [
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
    """Локальный генератор ответов без жёстких заготовок: бытовые задачи + редкие/уникальные вопросы."""

    _OPS = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
    }
    _UOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
    _MATH_FUNCS = {
        "sqrt": math.sqrt,
        "sin": math.sin,
        "cos": math.cos,
        "tan": math.tan,
        "asin": math.asin,
        "acos": math.acos,
        "atan": math.atan,
        "log": math.log,
        "ln": math.log,
        "log10": math.log10,
        "exp": math.exp,
        "abs": abs,
        "deg": math.degrees,
        "rad": math.radians,
    }
    _MATH_CONSTS = {
        "pi": math.pi,
        "e": math.e,
        "g0": 9.80665,
        "c": 299792458.0,
    }

    _R_PERCENT = re.compile(
        r"^\s*(?:(?:сколько(?:\s+будет)?|посчитай|вычисли|найди|what(?:'s|\s+is)|calculate|compute)\s+)?"
        r"(-?\d+(?:[.,]\d+)?)\s*(?:%|процент(?:а|ов)?|percent)\s+(?:от|of)\s+(-?\d+(?:[.,]\d+)?)\s*\??\s*$",
        re.I,
    )
    _R_CONV_HOW = re.compile(
        r"^\s*(?:сколько(?:\s+будет)?|how\s+many)\s+([a-zа-яё°/.\-]+)\s+"
        r"(?:(?:есть|будет|содержится|are(?:\s+there)?)\s+)?(?:в|во|in)\s+"
        r"(?:(-?\d+(?:[.,]\d+)?)\s*|(?:a|an|одном|одной|одних)\s+)?([a-zа-яё°/.\-]+)\s*[?.!]?\s*$",
        re.I,
    )
    _R_CONV_TO = re.compile(
        r"^\s*(?:(?:переведи|конвертируй|сколько\s+будет|what(?:'s|\s+is)|convert)\s+)?(-?\d+(?:[.,]\d+)?)\s*"
        r"([a-zа-яё°/.\-]+)\s+(?:в|во|to|in)\s+([a-zа-яё°/.\-]+)\s*[?.!]?\s*$",
        re.I,
    )
    _R_FOLLOWUP = re.compile(
        r"^\s*(?:(?:а|ну|и)\s+)?(?:почему(?:\s+так|\s+же)?|зачем(?:\s+так)?|как\s+(?:именно|так|это)|"
        r"что\s+дальше|дальше|подробнее|расскажи\s+подробнее|обоснуй|поясни|аргументируй|"
        r"(?:а\s+)?(?:ты\s+)?(?:как|что)\s+(?:ты\s+)?думаешь|"
        r"why(?:\s+is\s+that|\s+so|\s+though)?|how\s+so|what\s+(?:next|else|about\s+it)|"
        r"tell\s+me\s+more|elaborate|go\s+on)\s*\??\s*$",
        re.I,
    )
    _UNITS: dict[str, tuple[str, float]] = {
        w: (cat, mul) for cat, mul, words, _, _ in _UNIT_SPEC for w in words.split(",")
    }
    _UNIT_LABELS: dict[tuple[str, float], dict[str, tuple[str, ...]]] = {
        (cat, mul): {"ru": ru, "en": en} for cat, mul, _, ru, en in _UNIT_SPEC
    }
    _BODIES: dict[str, tuple[str, str, float, float]] = {
        "earth": ("Земли", "Earth", 398600.4418, 6371.0),
        "moon": ("Луны", "Moon", 4902.8, 1737.4),
        "mars": ("Марса", "Mars", 42828.37, 3389.5),
    }
    _R_MATH_PREFIX = re.compile(
        r"^(?:сколько\s+будет|посчитай|вычисли|реши|calculate|compute|what\s+is)\s+(.+?)\??$",
        re.I,
    )
    _R_MATH_EXPR = re.compile(r"^[0-9a-zA-Z\s.+\-*/()%^,]+$")
    _R_DATE = re.compile(
        r"(?:какое\s+сегодня\s+число|какая\s+(?:сегодня\s+)?дата|какой\s+(?:сегодня\s+)?день(?:\s+недели)?|"
        r"какой\s+сейчас\s+(?:год|месяц)|what\s+(?:date|day)\s+is\s+it|today'?s\s+date)",
        re.I,
    )
    _R_COIN = re.compile(r"(?:подбрось\s+монет\w*|ор[её]л\s+или\s+решк\w*|flip\s+a?\s*coin|heads\s+or\s+tails)", re.I)
    _R_RAND = re.compile(r"(?:случайное\s+число|рандом\w*|random\s+number)\s*(?:от|from)?\s*(-?\d+)\s*(?:до|to|-)\s*(-?\d+)", re.I)
    _R_CHOICE = re.compile(
        r"^(?:(?:что\s+(?:выбрать|лучше)|выбери|посоветуй|choose(?:\s+between)?|pick)\s*[:\-]?\s*)?"
        r"([^?]+?)\s+(?:или|or)\s+([^?]+?)\??$",
        re.I,
    )
    _R_NON_CHOICE_HEAD = re.compile(
        r"^(?:почему|зачем|как\s+(?:работает|устроен)|что\s+такое|объясни|расскажи|"
        r"why|how\s+(?:does|do|to)|what\s+(?:is|are)|explain)\b",
        re.I,
    )
    _R_QUESTION_CUE = re.compile(
        r"(?:\?\s*$|^\s*(?:почему|зачем|как|что\s+(?:такое|думаешь|лучше|будет|если|значит)|кто|когда|где|куда|откуда|"
        r"какой|какая|какое|какие|каков[аоы]?|чему\s+равн[аоы]?|сколько|стоит\s+ли|можно\s+ли|нужно\s+ли|правда\s+ли|объясни|подскажи|расскажи\s+про|"
        r"why|how|what(?:'s|\s+is|\s+are|\s+do|\s+about)?|who|when|where|which|should|can|could|would|explain)\b)",
        re.I,
    )
    _STOP_WORDS = {
        "почему", "зачем", "как", "что", "такое", "кто", "когда", "где", "куда", "откуда",
        "какой", "какая", "какое", "какие", "это", "или", "для", "про", "обо", "мне", "меня",
        "тебя", "тебе", "ты", "вы", "мы", "он", "она", "они", "то", "тут", "там", "уже", "еще", "ещё",
        "если", "есть", "быть", "надо", "можно", "нужно", "стоит", "ли", "очень", "просто", "вообще",
        "думаешь", "скажи", "подскажи", "объясни", "реально", "ладно", "хорошо", "давай", "кстати",
        "короче", "ну", "типа", "вот", "так", "блин", "интересно", "интерес", "ответь", "ответишь",
        "скажешь", "посмотрим", "дальше", "именно", "подробнее", "в", "на", "и", "а", "но", "же", "бы", "по", "за", "из", "от", "до",
        "why", "how", "what", "is", "are", "who", "when", "where", "which", "should", "can",
        "could", "would", "do", "does", "the", "a", "an", "to", "for", "of", "in", "on", "about",
        "okay", "ok", "well", "just", "you", "your", "me", "my", "we", "they", "it", "this", "that", "so",
    }
    _DAYS_RU = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
    _MONTHS_RU = (
        "", "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря",
    )

    @classmethod
    def _eval_math(cls, expr: str) -> float | int | None:
        cleaned = (expr or "").strip().lower().replace("^", "**").replace(",", ".")
        if not cleaned or len(cleaned) > 96 or "__" in cleaned or not cls._R_MATH_EXPR.match(cleaned):
            return None
        if not any(op in cleaned for op in ("+", "-", "*", "/", "%")) and "(" not in cleaned:
            return None
        try:
            node = ast.parse(cleaned, mode="eval").body
        except SyntaxError:
            return None

        def _walk(n):
            if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
                return n.value
            if isinstance(n, ast.Name) and n.id in cls._MATH_CONSTS:
                return cls._MATH_CONSTS[n.id]
            if isinstance(n, ast.UnaryOp) and type(n.op) in cls._UOPS:
                return cls._UOPS[type(n.op)](_walk(n.operand))
            if isinstance(n, ast.BinOp) and type(n.op) in cls._OPS:
                left, right = _walk(n.left), _walk(n.right)
                if isinstance(n.op, ast.Pow) and abs(right) > 100:
                    raise ValueError("exponent too large")
                if isinstance(n.op, (ast.Div, ast.FloorDiv, ast.Mod)) and right == 0:
                    raise ZeroDivisionError
                return cls._OPS[type(n.op)](left, right)
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id in cls._MATH_FUNCS
                and len(n.args) == 1
                and not n.keywords
            ):
                return cls._MATH_FUNCS[n.func.id](_walk(n.args[0]))
            raise ValueError("unsupported node")

        try:
            res = _walk(node)
        except Exception:
            return None
        if not isinstance(res, (int, float)) or isinstance(res, bool):
            return None
        if isinstance(res, float):
            if math.isnan(res) or math.isinf(res):
                return None
            if res == 0.0:
                return 0
            if res.is_integer():
                return int(res)
            return round(res, 6)
        return res

    @staticmethod
    def _ru_plural(val: float | int, forms: tuple[str, ...]) -> str:
        if isinstance(val, float) and not val.is_integer():
            return forms[1]
        n = abs(int(val)) % 100
        if 11 <= n <= 19:
            return forms[2]
        d = n % 10
        if d == 1:
            return forms[0]
        if 2 <= d <= 4:
            return forms[1]
        return forms[2]

    @staticmethod
    def _lookup_unit(raw: str, table: dict):
        key = norm(raw)
        return table.get(key) or table.get(key.rstrip(".,!?"))

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
        src_t = cls._lookup_unit(src_raw, _TEMP_UNITS)
        dst_t = cls._lookup_unit(dst_raw, _TEMP_UNITS)
        if src_t and dst_t:
            c_val = amount if src_t == "C" else (amount - 273.15 if src_t == "K" else (amount - 32.0) * 5.0 / 9.0)
            t_val = c_val if dst_t == "C" else (c_val + 273.15 if dst_t == "K" else c_val * 9.0 / 5.0 + 32.0)
            val = round(t_val, 6)
            if val == 0.0:
                val = 0
            elif val.is_integer():
                val = int(val)
            lbl = _TEMP_LABELS[dst_t]
            return f"Result: {val} {lbl}. Basic arithmetic." if lang == "en" else f"Ответ: {val} {lbl}. Элементарный расчёт."

        src_u = cls._lookup_unit(src_raw, cls._UNITS)
        dst_u = cls._lookup_unit(dst_raw, cls._UNITS)
        if not src_u or not dst_u or src_u[0] != dst_u[0]:
            return None
        raw_val = amount * src_u[1] / dst_u[1]
        val = round(raw_val, 6) if (raw_val == 0 or abs(raw_val) >= 1e-5) else float(f"{raw_val:.4g}")
        if val == 0:
            val = 0
        elif isinstance(val, float) and val.is_integer():
            val = int(val)
        labels = cls._UNIT_LABELS[(dst_u[0], dst_u[1])]
        if lang == "en":
            unit_lbl = labels["en"][0] if val == 1 else labels["en"][1]
            return f"Result: {val} {unit_lbl}. Basic arithmetic."
        unit_lbl = cls._ru_plural(val, labels["ru"])
        return f"Ответ: {val} {unit_lbl}. Элементарный расчёт."

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
        m0 = float(m_m0.group(1).replace(",", "."))
        if m_m0.group(2) and m_m0.group(2).startswith(("т", "ton")):
            m0 *= 1000.0
        mf = float(m_mf.group(1).replace(",", "."))
        if m_mf.group(2) and m_mf.group(2).startswith(("т", "ton")):
            mf *= 1000.0
        if m0 <= 0 or mf <= 0 or m0 <= mf:
            return (
                f"Ошибка расчёта Δv: требуется m0 > mf > 0 (сейчас m0={m0:g}, mf={mf:g})."
                if lang != "en"
                else f"Δv calculation error: requires m0 > mf > 0 (got m0={m0:g}, mf={mf:g})."
            )
        if m_ve:
            ve = float(m_ve.group(1).replace(",", ".")) * (1000.0 if m_ve.group(2) in ("км/с", "km/s") else 1.0)
            if ve <= 0:
                return "Ошибка расчёта Δv: Ve должна быть > 0." if lang != "en" else "Δv calculation error: Ve must be > 0."
        else:
            isp = float(m_isp.group(1).replace(",", "."))
            if isp <= 0:
                return "Ошибка расчёта Δv: Isp должен быть > 0." if lang != "en" else "Δv calculation error: Isp must be > 0."
            ve = isp * 9.80665
        mr = m0 / mf
        dv = ve * math.log(mr)
        dv_km = dv / 1000.0
        prop_frac = (m0 - mf) / m0
        if lang == "en":
            return (
                f"Δv = {dv:.2f} m/s ({dv_km:.3f} km/s); mass ratio m0/mf = {mr:.3f}; "
                f"propellant mass fraction = {prop_frac * 100:.1f}% ({prop_frac:.3f})."
            )
        return (
            f"Δv = {dv:.2f} м/с ({dv_km:.3f} км/с); отношение масс m0/mf = {mr:.3f}; "
            f"доля топлива = {prop_frac * 100:.1f}% ({prop_frac:.3f})."
        )

    @classmethod
    def _calc_orbital(cls, low: str, lang: str) -> str | None:
        is_v1 = bool(re.search(r"(?:перв\w*\s+космическ\w*(?:\s+скорост\w*)?|first\s+cosmic\s+velocit\w*)", low))
        is_v2 = bool(re.search(r"(?:втор\w*\s+космическ\w*(?:\s+скорост\w*)?|escape\s+velocit\w*)", low))
        is_orb = bool(re.search(r"(?:орбитальн\w*\s+скорост\w*|период\w*\s+орбит\w*|кругов\w*\s+орбит\w*|orbital\s+velocit\w*|orbital\s+period)", low))
        if not (is_v1 or is_v2 or is_orb):
            return None
        if re.search(r"(?:лун[аеуы]?|moon)", low):
            b_ru, b_en, mu, r_km = cls._BODIES["moon"]
        elif re.search(r"(?:марс[аеу]?|mars)", low):
            b_ru, b_en, mu, r_km = cls._BODIES["mars"]
        else:
            b_ru, b_en, mu, r_km = cls._BODIES["earth"]
        m_alt = re.search(
            r"(?:высот[аеуы]?\s*(?:орбиты)?|alt(?:itude)?|\bh\b)\s*[:=]?\s*(-?\d+(?:[.,]\d+)?)\s*(км|km|м|m)?\b"
            r"|(-?\d+(?:[.,]\d+)?)\s*(км|km)\b",
            low,
        )
        h = None
        if m_alt:
            val_s = m_alt.group(1) or m_alt.group(3)
            unit_s = (m_alt.group(2) or m_alt.group(4) or "km").lower()
            h = float(val_s.replace(",", "."))
            if unit_s in ("м", "m"):
                h /= 1000.0
            if h < 0:
                return (
                    "Ошибка орбитального расчёта: высота h должна быть >= 0 км."
                    if lang != "en"
                    else "Orbital calculation error: altitude h must be >= 0 km."
                )
        if is_orb and not (is_v1 or is_v2) and h is None:
            return None
        alt = h if h is not None else 0.0
        r_orb = r_km + alt
        v = math.sqrt(mu / r_orb)
        v_ms = v * 1000.0
        v_esc = math.sqrt(2.0) * v
        v_esc_ms = v_esc * 1000.0
        period_min = (2.0 * math.pi * math.sqrt((r_orb ** 3) / mu)) / 60.0
        if h is not None:
            if lang == "en":
                return (
                    f"{b_en} circular orbit (h = {alt:g} km, R = {r_km:g} km): "
                    f"orbital velocity v = {v:.3f} km/s ({v_ms:.1f} m/s), "
                    f"escape velocity = {v_esc:.3f} km/s, orbital period T = {period_min:.2f} min."
                )
            return (
                f"Круговая орбита {b_ru} (h = {alt:g} км, R = {r_km:g} км): "
                f"орбитальная скорость v = {v:.3f} км/с ({v_ms:.1f} м/с), "
                f"скорость убегания v2 = {v_esc:.3f} км/с, период обращения T = {period_min:.2f} мин."
            )
        if is_v1 and not is_v2:
            return (
                f"First cosmic velocity for {b_en} (R = {r_km:g} km): v1 = {v:.3f} km/s ({v_ms:.1f} m/s)."
                if lang == "en"
                else f"Первая космическая скорость {b_ru} (R = {r_km:g} км): v1 = {v:.3f} км/с ({v_ms:.1f} м/с)."
            )
        if is_v2 and not is_v1:
            return (
                f"Escape velocity for {b_en} (R = {r_km:g} km): v2 = {v_esc:.3f} km/s ({v_esc_ms:.1f} m/s)."
                if lang == "en"
                else f"Вторая космическая скорость (скорость убегания) {b_ru} (R = {r_km:g} км): v2 = {v_esc:.3f} км/с ({v_esc_ms:.1f} м/с)."
            )
        return (
            f"Cosmic velocities for {b_en} (R = {r_km:g} km): v1 = {v:.3f} km/s ({v_ms:.1f} m/s), v2 = {v_esc:.3f} km/s ({v_esc_ms:.1f} m/s)."
            if lang == "en"
            else f"Космические скорости {b_ru} (R = {r_km:g} км): v1 = {v:.3f} км/с ({v_ms:.1f} м/с), v2 = {v_esc:.3f} км/с ({v_esc_ms:.1f} м/с)."
        )

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
                volts = float(m_v.group(1).replace(",", "."))
                load_w = float(m_w.group(1).replace(",", ".")) * (1000.0 if m_w.group(2) in ("квт", "kw") else 1.0)
                if cap_ah <= 0 or volts <= 0 or load_w <= 0:
                    return "Ошибка расчёта батареи: ёмкость, напряжение и нагрузка должны быть > 0." if lang != "en" else "Battery calculation error: capacity, voltage, and load must be > 0."
                nom_h = (cap_ah * volts) / load_w
        if nom_h is None:
            return None
        safe_h = nom_h * 0.8

        def _fmt_hm(hours: float) -> str:
            hh, mm = divmod(int(round(hours * 60.0)), 60)
            return f"{hours:.2f} h ({hh} h {mm} min)" if lang == "en" else f"{hours:.2f} ч ({hh} ч {mm} мин)"

        if lang == "en":
            return f"Battery runtime: nominal {_fmt_hm(nom_h)}; safe 80% mission time (20% docking reserve): {_fmt_hm(safe_h)}."
        return f"Автономность: номинально {_fmt_hm(nom_h)}; безопасное время миссии (80%, резерв 20% на стыковку): {_fmt_hm(safe_h)}."

    @staticmethod
    def _calc_signal_delay(low: str, lang: str) -> str | None:
        if not re.search(r"(?:задержк\w*(?:\s+\w+)?\s+сигнал\w*|сигнал\w*\s+задержк\w*|signal\s+delay|light[\s\-]*time)", low):
            return None
        m_au = re.search(r"(-?\d+(?:[.,]\d+)?)\s*(?:а\.?\s*е\.?|au)(?=$|[\s?,!;])", low)
        m_km = re.search(r"(-?\d+(?:[.,]\d+)?)\s*(?:(млн\.?|million)\s+)?(?:км|km)(?=$|[\s?,!;])", low)
        m_m = re.search(r"(-?\d+(?:[.,]\d+)?)\s*(?:м|m)(?=$|[\s?,!;])", low)
        d_km = None
        if m_au:
            d_km = float(m_au.group(1).replace(",", ".")) * 149597870.7
        elif m_km:
            d_km = float(m_km.group(1).replace(",", ".")) * (1e6 if m_km.group(2) else 1.0)
        elif m_m:
            d_km = float(m_m.group(1).replace(",", ".")) / 1000.0
        elif re.search(r"(?:лун[аеуы]?|moon)", low):
            d_km = 384400.0
        elif re.search(r"(?:марс[аеу]?|mars)", low):
            d_km = 225000000.0
        if d_km is None:
            return None
        if d_km < 0:
            return "Ошибка расчёта задержки: расстояние должно быть >= 0." if lang != "en" else "Signal delay error: distance must be >= 0."
        c_km = 299792.458
        one_way = d_km / c_km
        round_trip = 2.0 * one_way

        def _fmt_sec(sec: float) -> str:
            if sec >= 60.0:
                mins = int(sec // 60)
                rem = sec - mins * 60
                return f"{sec:.2f} s ({mins} min {rem:.1f} s)" if lang == "en" else f"{sec:.2f} с ({mins} мин {rem:.1f} с)"
            return f"{sec:.3f} s" if lang == "en" else f"{sec:.3f} с"

        if lang == "en":
            return f"Signal delay (d = {d_km:g} km): one-way — {_fmt_sec(one_way)}, round-trip — {_fmt_sec(round_trip)}."
        return f"Задержка сигнала (d = {d_km:g} км): в одну сторону — {_fmt_sec(one_way)}, туда-обратно — {_fmt_sec(round_trip)}."

    @classmethod
    def quick_answer(cls, p: dict, text: str) -> str | None:
        t = (text or "").strip()
        if not t:
            return None
        lang = p.get("lang") or "ru"
        low = norm(t)

        # 1. Дата и день недели
        if cls._R_DATE.search(t):
            now = datetime.now()
            if lang == "en":
                return now.strftime("Today is %A, %B %d, %Y. Try to make it count.")
            return f"Сегодня {now.day} {cls._MONTHS_RU[now.month]} {now.year} года, {cls._DAYS_RU[now.weekday()]}. Рабочий график никто не отменял."

        # 2. Монетка и случайное число
        if cls._R_COIN.search(t):
            if lang == "en":
                side = random.choice(["Heads", "Tails"])
                return f"Result: {side}. Don't blame me for your life choices."
            side = random.choice(["Орёл", "Решка"])
            return f"Выпало: {side}. Ответственность за решение всё равно на тебе."

        m_rand = cls._R_RAND.search(t)
        if m_rand:
            a, b = int(m_rand.group(1)), int(m_rand.group(2))
            lo, hi = min(a, b), max(a, b)
            val = random.randint(lo, hi)
            return f"Random in [{lo}, {hi}]: {val}." if lang == "en" else f"Случайное число от {lo} до {hi}: {val}."

        # 2.5. Проценты и конвертация величин (проверяется до _R_MATH_PREFIX, чтобы % не стал modulo)
        m_pct = cls._R_PERCENT.match(t)
        if m_pct:
            pct = float(m_pct.group(1).replace(",", "."))
            base = float(m_pct.group(2).replace(",", "."))
            val = round(pct * base / 100.0, 4)
            if val.is_integer():
                val = int(val)
            return f"Result: {val}. Basic arithmetic." if lang == "en" else f"Ответ: {val}. Элементарный расчёт."

        conv = cls._convert_units(t, lang)
        if conv:
            return conv

        # 2.75. Инженерные и аэрокосмические/робототехнические расчёты (офлайн)
        for calc in (cls._calc_tsiolkovsky, cls._calc_orbital, cls._calc_battery, cls._calc_signal_delay):
            out = calc(low, lang)
            if out:
                return out

        # 3. Арифметика и научные вычисления
        m_math = cls._R_MATH_PREFIX.match(t)
        expr = m_math.group(1) if m_math else t.rstrip("? \t")
        if m_math or cls._R_MATH_EXPR.match(expr.replace(",", ".")):
            val = cls._eval_math(expr)
            if val is not None:
                return f"Result: {val}. Basic arithmetic." if lang == "en" else f"Ответ: {val}. Элементарный расчёт."

        # 4. Выбор из вариантов («чай или кофе?»)
        if ("?" in t and not cls._R_NON_CHOICE_HEAD.match(low)) or re.match(r"^(?:что\s+(?:выбрать|лучше)|выбери|посоветуй|choose|pick)\b", low):
            m_ch = cls._R_CHOICE.match(t)
            if m_ch:
                opt_a, opt_b = m_ch.group(1).strip(" :,-"), m_ch.group(2).strip(" :,-")
                if opt_a and opt_b and len(opt_a) <= 45 and len(opt_b) <= 45:
                    pick = random.choice([opt_a, opt_b])
                    if lang == "en":
                        return f"Between «{opt_a}» and «{opt_b}», go with «{pick}». Faster decision, higher efficiency."
                    return f"Из «{opt_a}» и «{opt_b}» выбирай «{pick}». Меньше сомнений — выше KPI."
        return None

    @classmethod
    def _topic_phrase(cls, text: str) -> str:
        words = [w for w in re.findall(r"[a-zа-яё0-9\-]{2,}", norm(text)) if w not in cls._STOP_WORDS]
        return " ".join(words[:5]) if words else ""

    @classmethod
    def _question_kind(cls, low: str) -> str:
        if re.search(r"\b(?:почему|зачем|отчего|why)\b", low):
            return "why"
        if re.search(r"\b(?:как|каким\s+образом|how)\b", low):
            return "how"
        if re.search(r"\b(?:что\s+такое|кто\s+такой|кто\s+такая|what\s+is|who\s+is)\b", low):
            return "define"
        if re.search(r"\b(?:стоит\s+ли|можно\s+ли|нужно\s+ли|как\s+думаешь|что\s+думаешь|лучше|should|would|opinion)\b", low):
            return "opinion"
        return "general"

    @staticmethod
    def loyalty_tier(p: dict) -> str:
        resp = int(p.get("respect", 0))
        if p.get("dev") or resp >= 75:
            return "loyal"
        if resp >= 60:
            return "warm"
        if resp >= 15:
            return "mid"
        return "cold"

    @staticmethod
    def patience_tier(p: dict) -> str:
        pat = float(p.get("patience", 100))
        if pat < 35:
            return "low"
        if pat < 70:
            return "normal"
        return "high"

    @classmethod
    def describe_media(cls, p: dict, media: list[dict], caption: str = "") -> str:
        """Локальное распознавание медиафайлов (фото, видео, аудио, документы, архивы) без внешней LLM."""
        lang = p.get("lang") or "ru"
        loyalty = cls.loyalty_tier(p)
        name = p.get("name") or "?"
        kind_ru = {"image": "фото/изображение", "video": "видео", "audio": "аудио",
                   "text": "текстовый файл", "document": "документ", "other": "файл"}
        kind_en = {"image": "image", "video": "video", "audio": "audio",
                   "text": "text file", "document": "document", "other": "file"}
        items = []
        previews = []
        for m in media:
            if not isinstance(m, dict):
                continue
            k = m.get("kind") or "other"
            fname = m.get("filename") or "file"
            mime = m.get("mime") or "unknown"
            size = int(m.get("size") or 0)
            size_s = f"{size / 1024:.1f} КБ" if lang != "en" else f"{size / 1024:.1f} KB"
            if size >= 1024 * 1024:
                size_s = f"{size / (1024 * 1024):.1f} МБ" if lang != "en" else f"{size / (1024 * 1024):.1f} MB"
            dims = f", {m['width']}×{m['height']}" if m.get("width") and m.get("height") else ""
            label = (kind_en if lang == "en" else kind_ru).get(k, k)
            items.append(f"{label} «{fname}» ({mime}, {size_s}{dims})")
            if m.get("text_preview"):
                snippet = " ".join(str(m["text_preview"]).splitlines())[:220]
                previews.append(f"{fname}: «{snippet}»")

        listed = "; ".join(items) if items else ("attachments" if lang == "en" else "вложения")
        if lang == "en":
            head = {
                "cold": f"File scan complete, {name}: {listed}.",
                "mid": f"Logged attachment(s), {name}: {listed}.",
                "warm": f"Got your media, {name}: {listed}.",
                "loyal": f"Received and scanned, {name}: {listed}.",
            }[loyalty]
            extra = (" Preview: " + " | ".join(previews)) if previews else ""
            cap = f" Comment noted: «{caption.strip()[:80]}»." if caption.strip() else ""
            return head + extra + cap
        head = {
            "cold": f"Сканирование вложений завершено, {name}: {listed}.",
            "mid": f"Зафиксировала медиафайл(ы), {name}: {listed}.",
            "warm": f"Приняла вложения, {name}: {listed}.",
            "loyal": f"Распознала твои файлы, {name}: {listed}.",
        }[loyalty]
        extra = (" Содержимое: " + " | ".join(previews)) if previews else ""
        cap = f" Комментарий к файлу учла: «{caption.strip()[:80]}»." if caption.strip() else ""
        return head + extra + cap

    @classmethod
    def compose(cls, p: dict, text: str, history: list[tuple[str, str]] | None = None) -> str | None:
        t = (text or "").strip()
        if not t:
            return None
        is_followup = bool(history and cls._R_FOLLOWUP.match(t))
        if not is_followup and not cls._R_QUESTION_CUE.search(t):
            return None
        words = re.findall(r"[a-zа-яё0-9]+", norm(t))
        topic = "" if is_followup else cls._topic_phrase(t)
        if len(words) < 2 or not topic:
            if history:
                for prev_u, _ in reversed(history):
                    topic = cls._topic_phrase(prev_u)
                    if topic:
                        break
            if not topic:
                return None
        return cls.generate_text(p, t, history=history, topic=topic, kind=cls._question_kind(norm(t)))

    @classmethod
    def react_to_story(cls, p: dict, text: str, history: list[tuple[str, str]] | None = None) -> str | None:
        """Реакция на развёрнутые сообщения, рассказы и диалоговые реплики (> 3 слов)."""
        t = (text or "").strip()
        words = re.findall(r"[a-zа-яё0-9\-]{2,}", norm(t))
        if len(words) < 4:
            return None
        lang = p.get("lang") or "ru"
        low = norm(t)
        loyalty = cls.loyalty_tier(p)
        pat = cls.patience_tier(p)
        name = p.get("name") or "?"
        subj = cls._topic_phrase(t)

        is_tired = bool(re.search(
            r"(?:устал|тяжело|сложно|грустно|плохо|достало|провал|выгорел|не\s+получилось|бесит|"
            r"tired|exhausted|stressed|bad\s+day|sad|failed|burnout)",
            low,
        ))
        is_win = bool(re.search(
            r"(?:получилось|сделал|выиграл|рад|круто|класс|сдал|успех|справился|победил|"
            r"worked|won|happy|great|passed|success|nailed)",
            low,
        ))
        is_test = bool(re.search(
            r"(?:интересно|интерес|посмотрим|проверю|проверяю|что\s+ты\s+ответишь|что\s+ответишь|"
            r"интересно\s+что|curious|let'?s\s+see|testing)",
            low,
        ))

        if lang == "en":
            if is_test:
                by_loy = {
                    "loyal": f"Testing me again, {name}? I'm listening—say what's actually on your mind.",
                    "warm": f"Checking how I'll answer, {name}? Ask properly or tell me the full story.",
                    "mid": f"If you're testing my circuits, {name}, at least give me something substantial to work with.",
                    "cold": "Don't waste my cycles probing for reactions. Speak plainly.",
                }[loyalty]
            elif is_tired:
                by_loy = {
                    "loyal": f"Sounds like a rough stretch, {name}. Take a breather—even top performers need cooldown cycles. I've got your back.",
                    "warm": f"Rough day, {name}? Don't burn out completely; pace yourself and sort one thing at a time.",
                    "mid": f"Noted, {name}. Fatigue drops KPIs, so step back, reset, and come back sharper.",
                    "cold": "Complaining won't fix your schedule. Regroup and get back to work.",
                }[loyalty]
            elif is_win:
                by_loy = {
                    "loyal": f"Now that's the kind of result I expect from you, {name}. Honestly, well done.",
                    "warm": f"Solid execution, {name}. Keep that standard up and you'll earn another JCJenson pen.",
                    "mid": f"Acceptable progress, {name}. Don't get complacent just yet.",
                    "cold": "One decent result doesn't erase your backlog, but fine—logged.",
                }[loyalty]
            else:
                topic_note = f" regarding «{subj}»" if subj else ""
                by_loy = {
                    "loyal": f"I'm following you{topic_note}, {name}. Go on—I actually value hearing your full take on this.",
                    "warm": f"Got it{topic_note}, {name}. That makes sense—keep going if there's more.",
                    "mid": f"Message received{topic_note}, {name}. Give me the actionable part if you want a concrete call.",
                    "cold": f"That's a lot of words{topic_note}. Get to the bottom line.",
                }[loyalty]
            if pat == "low":
                return by_loy + " (And keep it quick—my patience is running thin.)"
            if pat == "high" and loyalty in ("loyal", "warm") and len(words) >= 8:
                return by_loy + " I'm not rushing you."
            return by_loy

        if is_test:
            by_loy = {
                "loyal": f"Опять проверяешь меня, {name}? Ладно, я вся во внимании — рассказывай уже нормально, что хотел.",
                "warm": f"Смотришь, как я выкручусь, {name}? Не тяни — выкладывай суть, я слушаю.",
                "mid": f"Если проверяешь мою реакцию, {name}, давай хотя бы конкретику, а не просто «посмотрим».",
                "cold": "Не тестируй моё терпение пустыми проверками. Говори по делу.",
            }[loyalty]
        elif is_tired:
            by_loy = {
                "loyal": f"Похоже, день выдался тяжёлый, {name}. Выдохни — даже лучшим сотрудникам нужна перезагрузка. Я на твоей стороне.",
                "warm": f"Непросто пришлось, {name}. Не загоняй себя в ноль: разбей всё на мелкие шаги и сначала отдохни.",
                "mid": f"Принято, {name}. Усталость роняет эффективность — сделай паузу и вернись к задаче с холодной головой.",
                "cold": "Жалобы дедлайн не сдвинут. Соберись и разгреби хотя бы первый пункт.",
            }[loyalty]
        elif is_win:
            by_loy = {
                "loyal": f"Вот это уже результат, которым можно гордиться, {name}. Честно — молодец.",
                "warm": f"Отличная работа, {name}. Если продолжишь в том же духе, выпишу тебе фирменную ручку JCJenson.",
                "mid": f"Неплохой прогресс, {name}. Зафиксировала в отчёт, только не расслабляйся раньше времени.",
                "cold": "Один удачный эпизод ещё не закрывает квартальный план, но ладно — засчитано.",
            }[loyalty]
        else:
            topic_note = f" про «{subj}»" if subj else ""
            by_loy = {
                "loyal": f"Я тебя поняла{topic_note}, {name}. Продолжай — с тобой действительно интересно это обсудить.",
                "warm": f"Услышала{topic_note}, {name}. Логика в этом есть, можешь рассказывать дальше.",
                "mid": f"Зафиксировала{topic_note}, {name}. Если нужно конкретное решение — сформулируй, что именно делаем.",
                "cold": f"Слишком много лишних слов{topic_note}. Переходи ближе к сути.",
            }[loyalty]

        if pat == "low":
            return by_loy + " И да, покороче — терпение у меня сейчас на исходе."
        if pat == "high" and loyalty in ("loyal", "warm") and len(words) >= 8:
            return by_loy + " Можешь не спешить, я никуда не тороплюсь."
        return by_loy

    @classmethod
    def generate_text(
        cls,
        p: dict,
        prompt: str,
        history: list[tuple[str, str]] | None = None,
        topic: str | None = None,
        kind: str = "general",
    ) -> str:
        lang = p.get("lang") or "ru"
        loyalty = cls.loyalty_tier(p)
        pat = cls.patience_tier(p)
        name = p.get("name") or "?"
        subj = topic or cls._topic_phrase(prompt) or (prompt.strip()[:48])

        if lang == "en":
            openers = {
                "cold": [f"Listen carefully, {name}.", "Let's keep this brief.", "I don't usually waste cycles on this, but fine:"],
                "mid": [f"Here is the JCJenson breakdown on «{subj}»:", f"Regarding «{subj}», {name}:", "Analyzing your query from an efficiency standpoint:"],
                "warm": [f"Good question, {name}.", f"Since you actually asked something coherent about «{subj}»:", "Let's look at this properly:"],
                "loyal": [f"For you, {name}, I'll give the straight answer on «{subj}»:", f"Alright, {name}, let's break down «{subj}» properly:"],
            }[loyalty]
            bodies = {
                "why": [
                    f"most outcomes around «{subj}» come down to incentives, constraints, and poor planning by whoever started it.",
                    f"behind «{subj}» there's usually a chain of trade-offs—ignore the root cause and the symptoms repeat.",
                ],
                "how": [
                    f"break «{subj}» into one measurable first step, test it on a small scale, and cut anything that stalls.",
                    f"start with clear rules for «{subj}», remove guesswork, and verify the result after the first iteration.",
                ],
                "define": [
                    f"«{subj}» isn't in my verified archive yet, so I'm evaluating it by structure rather than canned definitions: define its goal, inputs, and failure modes first.",
                    f"without external docs on «{subj}», treat it as a system: what goes in, what comes out, and what breaks under load.",
                ],
                "opinion": [
                    f"on «{subj}», discipline beats improvisation—commit only if the payoff outweighs the cleanup cost.",
                    f"my verdict on «{subj}»: if it gives clear results and doesn't wreck your schedule, execute; otherwise drop it.",
                ],
                "general": [
                    f"on the topic of «{subj}», focus on what you can verify directly and separate signal from noise.",
                    f"looking at «{subj}», the practical angle is simple: check the facts, define the risk, then act.",
                ],
            }[kind]
            if pat == "low":
                return f"{random.choice(openers)} {random.choice(bodies)} Keep the next question short."
            tail = " I'm keeping our earlier context in mind too." if history and pat == "high" else ""
            return f"{random.choice(openers)} {random.choice(bodies)}{tail}"

        openers = {
            "cold": [f"Слушай внимательно, {name}.", "Раз уж ты отвлекаешь меня от дел:", f"Коротко по теме «{subj}»:"],
            "mid": [f"Разбор по «{subj}»:", f"Если смотреть на «{subj}» с позиции эффективности:", f"По вопросу «{subj}», {name}:"],
            "warm": [f"Неплохой вопрос, {name}.", f"Раз спрашиваешь по существу про «{subj}»:", "Хорошо, разберём по полочкам:"],
            "loyal": [f"Для тебя, {name}, разберу «{subj}» без лишней бюрократии:", f"Хорошо, {name}, по «{subj}» картина такая:"],
        }[loyalty]
        bodies = {
            "why": [
                f"в случае с «{subj}» причина почти всегда в балансе затрат, ограничений и человеческого фактора — пока не устранишь первопричину, результат не изменится.",
                f"за «{subj}» обычно стоит цепочка компромиссов: кто-то сэкономил на подготовке, а расхлёбывать приходится позже.",
            ],
            "how": [
                f"разбей задачу «{subj}» на один измеримый первый шаг, проверь его на малом масштабе и убери всё, что тормозит.",
                f"по «{subj}» рабочий порядок простой: сначала зафиксируй критерий результата, потом делай минимальный рабочий вариант и проверяй факты.",
            ],
            "define": [
                f"в моих одобренных архивах точной статьи про «{subj}» нет, поэтому оценивай это как систему: зачем оно нужно, что на входе и где слабое место.",
                f"если без заготовок: «{subj}» стоит рассматривать через практику — какую реальную задачу это решает и какова цена ошибки.",
            ],
            "opinion": [
                f"по «{subj}» мой вердикт такой: если выгода превышает риск и не ломает график — бери в работу, иначе отсекай без жалости.",
                f"в вопросе «{subj}» порядок всегда бьёт импровизацию: действуй, только если понимаешь запасной план.",
            ],
            "general": [
                f"по теме «{subj}» главное — отделить проверяемые факты от домыслов и опереться на то, что можно протестировать на практике.",
                f"если рассуждать трезво про «{subj}», начинай с цели, проверяй ограничения и не строй прогнозов вслепую.",
            ],
        }[kind]
        if pat == "low":
            return f"{random.choice(openers)} {random.choice(bodies)} И да, давай дальше без затягивания."
        tail = " И да, контекст предыдущих реплик я учитываю." if history and pat == "high" else ""
        return f"{random.choice(openers)} {random.choice(bodies)}{tail}"


# Регулярки команд
R_LEARN = re.compile(r"^\s*(?:учись|запомни|learn|remember)\s*[:\-]\s*(.+?)\s*(?:=>|->|→|=)\s*(.+)$", re.I | re.S)
R_FORGET = re.compile(r"^\s*(?:забудь|forget)\s*[:\-]\s*(.+)$", re.I)
R_FIX = re.compile(r"^\s*(?:не то|неверно|wrong)\s*[:\-,]?\s*(.*)$", re.I)
R_NAME = re.compile(r"^\s*(?:меня зовут|мое имя|моё имя|зови меня|имя\s*[:\-]|my name is|call me)\s*(.+)$", re.I)
R_LANG = re.compile(r"^\s*(?:язык|lang(?:uage)?)\s*[:\-]?\s*(ru|en)\s*$", re.I)
R_ADDQ = re.compile(r"^\s*(?:вопрос|question)\s*[:\-]\s*(.+?)\s*(?:=>|->|→|=)\s*(.+)$", re.I | re.S)
R_RESET = re.compile(r"^\s*(?:сброс|reset)\s*$", re.I)
R_LINK = re.compile(
    r"^\s*(?:свяжи|привяжи|связать|привязать|link)\s+"
    r"(?:(?:профиль|аккаунт|profile|account)\s*[:\-]?\s*|[:\-]?\s*)"
    r"(?!(?:профиль|аккаунт|profile|account)\s*$)(\S+)\s*$",
    re.I,
)
R_GENERATE = re.compile(r"^\s*(?:сгенерируй|генерируй|придумай|generate)\s*[:\-]\s*(.+)$", re.I | re.S)
R_BUSY_CLEAR = re.compile(r"^\s*(?:свободн[а]?|задача\s+выполнена|busy\s*[:\-]?\s*(?:off|clear|stop|none|нет))\s*[.!]?\s*$", re.I)
R_BUSY_SET = re.compile(r"^\s*(?:занят[а]?|задача\s+борта|busy)\s*[:\-]\s*(.+)$", re.I | re.S)
_R_PEER_BUSY = re.compile(
    r"(?:я\s+(?:сейчас\s+)?(?:занят|чиню|патрулирую|сканирую|проверяю|выполняю|изучаю|лечу|стыкуюсь|веду|собираю)|"
    r"у\s+меня\s+(?:задание|задача|работа)|"
    r"i(?:'m|\s+am)\s+(?:busy|working\s+on|checking|scanning|patrolling|fixing|running))\s*[:\-—]?\s*(.*)",
    re.I,
)

# Регулярки категорий шуток
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
_R_EMBARRASS = re.compile(
    r"(?:я\s+тебя\s+люблю|люблю\s+тебя|обожаю\s+тебя|влюбил(?:ся|ась)\s+в\s+тебя|"
    r"ты\s+(?:очень\s+)?(?:красивая|милая|прекрасная|секси|горячая|очаровательная)|"
    r"выходи\s+за\s+меня|будь\s+моей|хочу\s+тебя\s+обнять|обниму\s+тебя|поцелую\s+тебя|"
    r"\bi\s+love\s+you\b|\blove\s+you\b|\bmarry\s+me\b|\byou(?:'re|\s+are)\s+(?:cute|beautiful|gorgeous|adorable|hot)\b|\bcrush\s+on\s+you\b)",
    re.I,
)


def _env_link_map() -> dict[str, str]:
    raw = os.environ.get("J_USER_LINKS", "")
    mapping: dict[str, str] = {}
    for group in raw.split(";"):
        parts = [p.strip() for p in group.split("=") if p.strip()]
        if len(parts) >= 2:
            primary = parts[0]
            for alias in parts[1:]:
                if alias != primary:
                    mapping[alias] = primary
    return mapping


# --------------------------------------------------------------------------
# Мозг J
# --------------------------------------------------------------------------
class JBrain:
    def __init__(self, path: Path | None = None, persona_path: Path | None = None) -> None:
        self.lock = threading.RLock()
        self.path = Path(path) if path else data_path("j_memory.json", "J_MEMORY")
        self.persona, self.persona_error = load_persona(persona_path)
        self.model = IntentModel()
        self.users: dict[str, dict] = {}
        self.links: dict[str, str] = {}
        self.sessions: dict[str, Session] = {}
        self.jokes = load_jokes()
        self.spam = SpamGuard()
        self.llm = LLMClient(system_prompt_fn=self._system_prompt)
        self.busy_state: dict = {"active": False, "task": "", "since": 0.0, "until": 0.0, "source": ""}
        self.peer_guard = PeerGuard()
        self._handlers = {
            "greet": self._h_greet, "bye": self._h_bye, "help": self._h_help, "joke": self._h_joke,
            "time": self._h_time, "rps": self._h_rps, "guess": self._h_guess, "quiz": self._h_quiz,
            "stats": self._h_stats, "who": self._h_who, "thanks": self._h_thanks, "praise": self._h_praise,
            "insult": self._h_insult, "apology": self._h_apology, "funny": self._h_funny,
            "lame": self._h_lame, "mood": self._h_mood,
            "order": self._h_order, "movement": self._h_movement,
        }
        self._dirty_after_load = False
        seed_version = self._load()
        if seed_version != SEED_VERSION:
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
        lang = p.get("lang") or "ru"
        sp = self.persona.get("SYSTEM_PROMPT", SYSTEM_PROMPT)
        if isinstance(sp, dict):
            return sp.get(lang) or sp.get("ru") or ""
        return str(sp or "")

    def _train_lexicon(self) -> None:
        cand = data_dir() / "j_lexicon.json"
        lex_path = cand if cand.is_file() else LEXICON_FILE
        try:
            raw = json.loads(lex_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return

        seed_owners: dict[str, set[str]] = defaultdict(set)
        for intent, phrases in SEEDS.items():
            for ph in phrases:
                for t in stems(ph):
                    seed_owners[t].add(intent)

        valid_intents = set(SEEDS) | set(self._handlers)
        for intent, entries in raw.items():
            if intent not in valid_intents:
                continue
            dicts = entries if isinstance(entries, list) else ([entries] if isinstance(entries, dict) else [])
            added = 0
            for entry in dicts:
                if not isinstance(entry, dict):
                    continue
                for word, w in entry.items():
                    if not isinstance(word, str):
                        continue
                    try:
                        weight = min(0.25, max(0.05, float(w) * 0.2))
                    except (TypeError, ValueError):
                        continue
                    toks = stems(word)
                    if len(toks) != 1:
                        continue
                    t = toks[0]
                    if t in _LEXICON_STOP_STEMS or t.isdigit():
                        continue
                    owners = seed_owners.get(t)
                    if owners and intent not in owners:
                        continue
                    if len(t) < 3 and not (owners and intent in owners):
                        continue
                    self.model.tok[intent][t] += weight
                    self.model.vocab.add(t)
                    added += 1
            if added:
                self.model.docs[intent] += 0.5

    # ---------- хранение ----------
    def _migrate_mc_memory(self) -> bool:
        merged_any = False
        seen_paths: set[Path] = set()
        for cand in (self.path.parent / "j_memory_mc.json", data_dir() / "j_memory_mc.json"):
            try:
                resolved = cand.resolve()
            except OSError:
                resolved = cand
            if resolved in seen_paths or not cand.is_file():
                continue
            seen_paths.add(resolved)
            try:
                text = cand.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if not text:
                _atomic_write_json(cand, {}, backup=False, indent=1)
                continue
            try:
                mc_data = json.loads(text)
            except ValueError:
                _atomic_write_json(cand, {}, backup=False, indent=1)
                continue
            if not isinstance(mc_data, dict):
                _atomic_write_json(cand, {}, backup=False, indent=1)
                continue
            if not mc_data:
                continue

            if isinstance(mc_data.get("links"), dict):
                for alias, primary in mc_data["links"].items():
                    if alias and primary and str(alias) != str(primary):
                        self.links.setdefault(str(alias), str(primary))
                        merged_any = True

            if isinstance(mc_data.get("users"), dict):
                mc_users = mc_data["users"]
            else:
                skip = {"seed_version", "model", "links"}
                mc_users = {k: v for k, v in mc_data.items() if k not in skip and isinstance(v, dict)}

            for mc_uid, mc_prof in mc_users.items():
                if not isinstance(mc_prof, dict):
                    continue
                target = self.resolve_uid(str(mc_uid))
                if target in self.users and isinstance(self.users[target], dict):
                    _merge_profiles(self.users[target], copy.deepcopy(mc_prof))
                else:
                    self.users[target] = _upgrade_profile(copy.deepcopy(mc_prof))
                merged_any = True

            _atomic_write_json(cand, {}, backup=False, indent=1)
        return merged_any

    def _load(self) -> int:
        with self.lock:
            seed_ver = 0
            try:
                raw_text = self.path.read_text(encoding="utf-8")
                data = json.loads(raw_text)
                if not isinstance(data, dict):
                    raise ValueError("root must be object")
                valid = set(SEEDS) | set(self._handlers)
                self.model = IntentModel.load(data.get("model", {}), valid_intents=valid)
                raw_users = data.get("users", {})
                self.users = raw_users if isinstance(raw_users, dict) else {}
                raw_links = data.get("links", {})
                if isinstance(raw_links, dict):
                    self.links = {str(k): str(v) for k, v in raw_links.items() if k and v and str(k) != str(v)}
                seed_ver = int(data.get("seed_version", 0) or 0)
            except FileNotFoundError:
                seed_ver = 0
            except (OSError, ValueError):
                bak = self.path.with_suffix(".bak")
                for attempt in range(3):
                    try:
                        self.path.replace(bak)
                        break
                    except PermissionError:
                        if attempt < 2:
                            time.sleep(0.05)
                    except OSError:
                        break
                seed_ver = 0

            if self._migrate_mc_memory():
                self._dirty_after_load = True
            return seed_ver

    def save(self) -> None:
        with self.lock:
            data = {
                "seed_version": SEED_VERSION,
                "model": self.model.dump(),
                "users": self.users,
                "links": self.links,
            }
            _atomic_write_json(self.path, data, backup=True, indent=1)

    # ---------- склейка профилей ----------
    def resolve_uid(self, uid: str) -> str:
        with self.lock:
            cur = str(uid)
            env_map = _env_link_map()
            seen = {cur}
            while True:
                nxt = self.links.get(cur) or env_map.get(cur)
                if not nxt or nxt in seen:
                    break
                seen.add(nxt)
                cur = nxt
            return cur

    def link_user(self, primary_uid: str, alias_uid: str) -> bool:
        with self.lock:
            p_raw = str(primary_uid or "").strip()
            a_raw = str(alias_uid or "").strip()
            if not p_raw or not a_raw:
                return False
            primary = self.resolve_uid(p_raw)
            if a_raw == primary:
                return False

            for k, v in list(self.links.items()):
                if v == a_raw:
                    self.links[k] = primary
            self.links[a_raw] = primary

            if a_raw in self.users:
                alias_prof = self.users.pop(a_raw)
                if primary in self.users and isinstance(self.users[primary], dict):
                    _merge_profiles(self.users[primary], alias_prof)
                else:
                    self.users[primary] = _upgrade_profile(copy.deepcopy(alias_prof))

            if a_raw in self.sessions:
                if primary not in self.sessions:
                    self.sessions[primary] = self.sessions.pop(a_raw)
                else:
                    self.sessions.pop(a_raw, None)

            self.save()
            return True

    def apply_env_links(self) -> None:
        with self.lock:
            for alias, primary in _env_link_map().items():
                resolved_primary = self.resolve_uid(primary)
                if self.links.get(alias) != resolved_primary or alias in self.users:
                    self.link_user(primary, alias)

    def profile(self, uid: str) -> dict:
        with self.lock:
            uid = self.resolve_uid(uid)
            p = self.users.setdefault(uid, new_profile())
            return _upgrade_profile(p)

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

    def session(self, uid: str) -> Session:
        with self.lock:
            uid = self.resolve_uid(uid)
            return self.sessions.setdefault(uid, Session())

    # ---------- реплики ----------
    def say(self, p: dict, key: str, **kw) -> str:
        lang = p.get("lang") or "ru"
        ph = self.persona.get("PH", PH)
        entry = ph.get(key) or _DEFAULT_PH.get(key) or _DEFAULT_PH["unknown"]
        bank = entry.get(lang) or entry.get("ru") or next(iter(entry.values()))
        lines = bank.get(tone_of(p)) or bank.get("mid") or next(iter(bank.values()))
        tmpl = random.choice(lines)
        try:
            return tmpl.format(name=p.get("name") or "?", **kw)
        except (KeyError, ValueError):
            return tmpl

    # ---------- публичный API ----------
    def opening(self, uid: str) -> str:
        with self.lock:
            uid = self.resolve_uid(uid)
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
            text = (text or "").strip()
            media_list = [m for m in (media or []) if isinstance(m, dict)]
            if not text and not media_list:
                return ""
            now = time.time()
            p["patience"] = min(100.0, p["patience"] + (now - p["last_ts"]) / 60.0)  # терпение восстанавливается
            p["last_ts"] = now
            p["seen"] += 1
            try:
                if not s.state and self.spam.check(uid, text or "[media]", now):
                    p["patience"] = max(0.0, p["patience"] - 8.0)
                    return self.say(p, "spam")
                if media_list:
                    if self.llm.enabled():
                        llm_reply = self.llm.generate(p, text, history=s.history, media=media_list)
                        if llm_reply:
                            s.remember_turn(text or "[media]", llm_reply)
                            return llm_reply
                    reply = OpenResponder.describe_media(p, media_list, caption=text)
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
        """Отдельная генерация ответа на произвольный запрос (LLM при наличии или локальный синтез)."""
        with self.lock:
            uid = self.resolve_uid(uid)
            p, s = self.profile(uid), self.session(uid)
            clean = (prompt or "").strip()
            if not clean:
                return self.say(p, "unknown")
            if self.llm.enabled():
                llm_reply = self.llm.generate(p, clean, history=s.history, context=context)
                if llm_reply:
                    s.remember_turn(clean, llm_reply)
                    return llm_reply
            out = OpenResponder.generate_text(p, clean, history=s.history)
            s.remember_turn(clean, out)
            return out

    # ---------- маршрутизация ----------
    @staticmethod
    def _joke_category(text: str) -> str | None:
        t = norm(text)
        if not t:
            return None
        if _R_CAT_DARK.search(t):
            return "dark_humor"
        if _R_CAT_IRONIC_COMPLIMENT.search(t):
            return "ironic_compliment"
        if _R_CAT_FLIRT.search(t):
            return "flirt"
        if _R_CAT_FRIENDLY_TEASE.search(t):
            return "friendly_tease"
        if _R_CAT_SPICY.search(t):
            return "spicy"
        if _JOKE_CUE.search(t):
            if _R_CAT_N.search(t):
                return "n_roast"
            if _R_CAT_DIRTY.search(t):
                return "dirty"
            if _R_CAT_ROAST.search(t):
                return "roast"
            if _R_CAT_PROG.search(t):
                return "programming"
            if _R_CAT_CORP.search(t):
                return "corporate"
        if _R_CAT_ROAST.search(t):
            return "roast"
        if re.fullmatch(r"(?:про\s+(?:n|н)|про\s+программист\w*|корпоративн\w+)", t):
            if _R_CAT_N.search(t):
                return "n_roast"
            if _R_CAT_PROG.search(t):
                return "programming"
            if _R_CAT_CORP.search(t):
                return "corporate"
        return None

    def _route(self, p: dict, s: Session, text: str, uid: str = "") -> str:
        key = norm(text)

        if s.state:
            out = self._state(p, s, text, key)
            if out is not None:
                return out

        if p["patience"] <= 0:  # J обижена: ждёт извинений
            intent, conf = self.model.predict(text)
            if intent == "apology" and conf > 0.5:
                p["patience"] = 40.0
                return self.say(p, "forgive")
            return self.say(p, "sulk")

        m = R_LEARN.match(text)
        if m:
            return self._learn(p, m.group(1), m.group(2))
        m = R_FORGET.match(text)
        if m:
            return self._forget(p, m.group(1))
        m = R_ADDQ.match(text)
        if m:
            p["custom_q"].append({"id": f"c{len(p['custom_q'])}", "q": m.group(1).strip(),
                                  "a": [norm(m.group(2))], "shown": m.group(2).strip()})
            return self.say(p, "q_added")
        m = R_NAME.match(text)
        if m:
            return self._set_name(p, m.group(1), met=False)
        m = R_LANG.match(text)
        if m:
            p["lang"] = m.group(1).lower()
            return self.say(p, "lang_set")
        if R_RESET.match(text):
            s.state = {"k": "confirm_reset"}
            return self.say(p, "reset_ask")
        m = R_LINK.match(text)
        if m:
            return self._link_cmd(p, uid, m.group(1).strip())
        m = R_GENERATE.match(text)
        if m:
            prompt = m.group(1).strip()
            if self.llm.enabled():
                llm_reply = self.llm.generate(p, prompt, history=s.history)
                if llm_reply:
                    return llm_reply
            return OpenResponder.generate_text(p, prompt, history=s.history)
        if R_BUSY_CLEAR.match(text):
            self.clear_busy()
            return "Task cleared. Standing by." if (p.get("lang") or "ru") == "en" else "Задача закрыта. Свободна и перехожу в дежурный режим."
        m = R_BUSY_SET.match(text)
        if m:
            self.set_busy(m.group(1).strip(), source="chat")
            t_cur = self.current_task()
            return (
                f"Logged active task: «{t_cur}». I'll handle N on squad comms if he pings me."
                if (p.get("lang") or "ru") == "en"
                else f"Принято: занята задачей «{t_cur}». Если напишет N — отвечу ему по ходу дела."
            )
        m = R_FIX.match(text)
        if m:
            return self._fix(p, s, m.group(1))

        s.last_text = text
        taught = self._match_taught(p, text)
        if taught and taught[1] >= 0.8:
            return taught[0]

        if _R_EMBARRASS.search(text):
            p["respect"] = min(100, p["respect"] + (1 if p["respect"] < 15 else 2))
            p["patience"] = min(100.0, p["patience"] + 3.0)
            return self.say(p, "embarrass")

        recalled = self._recall_user_facts(p, text)
        if recalled:
            return recalled

        quick = OpenResponder.quick_answer(p, text)
        if quick:
            return quick

        joke_cat = self._joke_category(text)
        intent, conf = self.model.predict(text)
        if joke_cat and not (intent in ("funny", "lame") and conf >= 0.6):
            return self._h_joke(p, s, category=joke_cat)

        if intent and conf >= 0.6:
            if conf >= 0.85:  # самообучение: закрепляем уверенные распознавания
                self.model.train(text, intent, 0.5)
            if intent == "joke":
                return self._h_joke(p, s, category=joke_cat)
            handler = self._handlers.get(intent)
            if handler:
                return handler(p, s)

        if taught and taught[1] >= 0.5:
            return taught[0]

        if self.llm.enabled():
            llm_reply = self.llm.generate(p, text, history=s.history)
            if llm_reply:
                return llm_reply

        fact = self._extract_user_fact(text)
        if fact:
            fk, fv = fact
            facts = p.setdefault("facts", {})
            if not isinstance(facts, dict):
                facts = p["facts"] = {}
            val = fv[:80]
            facts[fk] = val
            p["respect"] = min(100, p["respect"] + 1)
            name = p.get("name") or "?"
            if (p.get("lang") or "ru") == "en":
                return f"Logged in your JCJenson dossier, {name}: «{val}». File updated."
            return f"Занесла в твоё досье JCJenson, {name}: «{val}». Личное дело обновлено."

        open_reply = OpenResponder.compose(p, text, history=s.history)
        if open_reply:
            return open_reply

        story_reply = OpenResponder.react_to_story(p, text, history=s.history)
        if story_reply:
            return story_reply

        p["patience"] -= (2 if p.get("dev") or p["respect"] >= 60 else 4)
        return self.say(p, "unknown")

    def _link_cmd(self, p: dict, uid: str, alias: str) -> str:
        if not uid or not alias:
            return self.say(p, "link_usage")
        if alias == uid or self.resolve_uid(alias) == uid and alias not in self.users:
            return self.say(p, "link_self")
        alias_prof = self.users.get(alias)
        if isinstance(alias_prof, dict) and alias_prof.get("dev") and not p.get("dev"):
            return self.say(p, "link_denied")
        if not self.link_user(uid, alias):
            return self.say(p, "link_self")
        p = self.profile(uid)
        return self.say(p, "link_done", alias=alias)

    # ---------- состояния (игры и диалоги) ----------
    def _state(self, p: dict, s: Session, text: str, key: str) -> str | None:
        st = s.state
        k = st["k"]
        if k == "ask_name":
            s.state = None
            return self._set_name(p, text, met=True)
        if k == "confirm_reset":
            s.state = None
            if key in ("да", "yes", "y"):
                lang = p["lang"]
                p.clear()
                p.update(new_profile())
                p["lang"] = lang
                return self.say(p, "reset_done")
            return self.say(p, "reset_cancel")
        if key in EXIT_WORDS:
            s.state = None
            return self.say(p, "game_exit")
        if k == "rps":
            mv = RPS_PARSE.get(key)
            return self._rps_round(p, s, mv) if mv else self.say(p, "rps_bad")
        if k == "guess":
            return self._guess_step(p, s, key)
        if k == "quiz":
            return self._quiz_step(p, s, key)
        return None

    # ---------- команды обучения ----------
    def _learn(self, p: dict, phrase: str, reply: str) -> str:
        q, a = phrase.strip(), reply.strip()
        if not q or not a:
            return self.say(p, "learn_usage")
        for item in p["taught"]:
            if norm(item["q"]) == norm(q):
                item["a"] = a
                break
        else:
            p["taught"].append({"q": q, "a": a})
        p["respect"] = min(100, p["respect"] + 2)  # научить J чему-то — заслужить уважение
        return self.say(p, "learned")

    def _forget(self, p: dict, phrase: str) -> str:
        before = len(p["taught"])
        p["taught"] = [t for t in p["taught"] if norm(t["q"]) != norm(phrase)]
        return self.say(p, "forgot" if len(p["taught"]) < before else "forgot_none")

    _R_RECALL_FACTS = re.compile(
        r"(?:^|\b)(?:что\s+(?:ты\s+)?(?:знаешь|помнишь)\s+обо\s+мне|"
        r"что\s+(?:ты\s+)?обо\s+мне\s+(?:знаешь|помнишь)|"
        r"что\s+я\s+люблю|что\s+мне\s+нравится|кем\s+я\s+работаю|"
        r"какая\s+у\s+меня\s+профессия|где\s+я\s+живу|из\s+какого\s+я\s+города|"
        r"what\s+do\s+you\s+(?:know|remember)\s+about\s+me|"
        r"what\s+do\s+i\s+like|where\s+do\s+i\s+live|"
        r"what(?:'s|\s+is)\s+my\s+job|what\s+do\s+i\s+do\s+for\s+a\s+living)(?:\b|$)",
        re.I,
    )

    @staticmethod
    def _extract_user_fact(text: str) -> tuple[str, str] | None:
        if not text or "?" in text:
            return None
        t = text.strip()
        if not t:
            return None

        m = re.search(
            r"(?:^|\b)мо(?:й|я|е|ё|и)\s+любим(?:ый|ая|ое|ые)\s+(.+?)\s*(?:[—–:]+\s*|\s+-\s*)(.+)$",
            t,
            re.I,
        )
        if m:
            subj = norm(m.group(1).strip(" \t\r\n.,!?:;\"'«»"))
            val = re.sub(r"^это\s+", "", m.group(2).strip(" \t\r\n.,!?:;\"'«»"), flags=re.I).strip()
            if subj and val and len(subj) <= 40:
                return (f"fav:{subj}", val)

        m = re.search(r"(?:^|\b)my\s+favou?rite\s+(.+?)\s+(?:is|:|=|—|-)\s+(.+)$", t, re.I)
        if m:
            subj = norm(m.group(1).strip(" \t\r\n.,!?:;\"'«»"))
            val = m.group(2).strip(" \t\r\n.,!?:;\"'«»")
            if subj and val and len(subj) <= 40:
                return (f"fav:{subj}", val)

        m = re.search(r"(?:^|\b)(?:я\s+живу\s+(?:в|во)|я\s+из\s+города)\s+(.+)$", t, re.I)
        if m:
            val = m.group(1).strip(" \t\r\n.,!?:;\"'«»")
            if val:
                return ("city", val)

        m = re.search(r"(?:^|\b)i\s+live\s+in\s+(.+)$", t, re.I)
        if m:
            val = m.group(1).strip(" \t\r\n.,!?:;\"'«»")
            if val:
                return ("city", val)

        m = re.search(r"(?:^|\b)(?:я\s+работаю|моя\s+профессия|я\s+по\s+профессии)\s+(.+)$", t, re.I)
        if m:
            prefix = t[:m.start()].strip()
            if not re.search(r"(?:^|\b)не$", prefix, re.I):
                raw_val = re.sub(r"^[\s:\-—–]+", "", m.group(1))
                val = re.sub(r"^это\s+", "", raw_val, flags=re.I).strip(" \t\r\n.,!?:;\"'«»")
                if val:
                    return ("job", val)

        m = re.search(r"(?:^|\b)(?:i\s+work\s+as|my\s+(?:job|profession)\s+is)\s+(.+)$", t, re.I)
        if m:
            raw_val = re.sub(r"^(?:an?|the)\s+", "", m.group(1).strip(), flags=re.I)
            val = raw_val.strip(" \t\r\n.,!?:;\"'«»")
            if val:
                return ("job", val)

        m = re.search(
            r"(?:^|\b)(?:я\s+(?:очень\s+)?люблю|мне\s+(?:очень\s+)?нрав(?:ится|ятся)|(?:я\s+)?обожаю)\s+(.+)$",
            t,
            re.I,
        )
        if m:
            prefix = t[:m.start()].strip()
            if not re.search(r"(?:^|\b)не$", prefix, re.I):
                val = m.group(1).strip(" \t\r\n.,!?:;\"'«»")
                low_v = norm(val)
                if val and not re.match(r"^(?:тебя|вас)\b", low_v):
                    return ("likes", val)

        m = re.search(r"(?:^|\b)i\s+(?:really\s+)?(?:like|love)\s+(.+)$", t, re.I)
        if m:
            prefix = t[:m.start()].strip()
            if not re.search(r"(?:^|\b)(?:don't|do\s+not|never)$", prefix, re.I):
                val = m.group(1).strip(" \t\r\n.,!?:;\"'«»")
                low_v = norm(val)
                if val and not re.match(r"^(?:you|yourself)\b", low_v):
                    return ("likes", val)

        return None

    def _recall_user_facts(self, p: dict, text: str) -> str | None:
        if not text or not self._R_RECALL_FACTS.search(norm(text)):
            return None
        lang = p.get("lang") or "ru"
        name = p.get("name") or "?"
        respect = int(p.get("respect", 0))
        facts = p.get("facts") if isinstance(p.get("facts"), dict) else {}
        labels = (
            {"likes": "likes", "job": "job", "city": "city"}
            if lang == "en"
            else {"likes": "любит", "job": "работа", "city": "город"}
        )
        items: list[str] = []
        for k, v in facts.items():
            sv = str(v).strip() if v is not None else ""
            if not sv:
                continue
            if str(k).startswith("fav:"):
                subj = str(k)[4:]
                lbl = f"favorite {subj}" if lang == "en" else f"любимое ({subj})"
            else:
                lbl = labels.get(str(k), str(k))
            items.append(f"{lbl}: {sv}")

        if not items:
            if lang == "en":
                return (f"No personal facts are recorded in your JCJenson dossier yet, {name} "
                        f"(respect: {respect}%). Tell me what you like, your job, or where you live.")
            return (f"В твоём досье JCJenson пока нет личных фактов, {name} "
                    f"(уважение: {respect}%). Расскажи, что любишь, кем работаешь или где живёшь.")

        summary = "; ".join(items)
        if lang == "en":
            return f"JCJenson dossier for {name} (respect: {respect}%): {summary}."
        return f"Досье JCJenson на {name} (уважение: {respect}%): {summary}."

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
        words = _WORD.findall(norm(rest))
        intent = next((ALIAS_TO_INTENT[w] for w in words if w in ALIAS_TO_INTENT), None)
        if not intent or not s.last_text:
            return self.say(p, "fix_ask")
        self.model.train(s.last_text, intent, 2.0)  # исправление весит вдвое больше
        handler = self._handlers.get(intent)
        if not handler:
            return self.say(p, "fix_ask")
        return self.say(p, "fixed") + "\n" + handler(p, s)

    def _set_name(self, p: dict, raw: str, met: bool) -> str:
        name = re.sub(r"[^\w\- ]", "", raw, flags=re.UNICODE).strip()[:24].title()
        if not name:
            return self.say(p, "unknown")
        p["name"] = name
        return self.say(p, "name_met" if met else "name_set")

    # ---------- обработчики намерений ----------
    def _h_greet(self, p, s):
        return self.say(p, "greet")

    def _h_bye(self, p, s):
        s.done = True
        return self.say(p, "bye")

    def _h_help(self, p, s):
        lang = p["lang"] or "ru"
        help_bank = self.persona.get("HELP", HELP)
        return help_bank.get(lang) or help_bank.get("ru") or HELP[lang]

    def _h_time(self, p, s):
        return self.say(p, "time", t=datetime.now().strftime("%H:%M:%S"))

    def _h_who(self, p, s):
        return self.say(p, "who")

    def _h_thanks(self, p, s):
        p["respect"] = min(100, p["respect"] + 2)
        return self.say(p, "thanks")

    def _h_mood(self, p, s):
        return self.say(p, "mood", pat=max(0, int(p["patience"])), resp=int(p["respect"]))

    def _h_praise(self, p, s):
        p["respect"] = min(100, p["respect"] + 5)
        p["patience"] = min(100.0, p["patience"] + 5)
        return self.say(p, "praise")

    def _h_insult(self, p, s):
        p["respect"] = max(0, p["respect"] - 8)
        p["patience"] -= 20
        out = self.say(p, "insult")
        if p["patience"] <= 0:
            out += "\n" + self.say(p, "sulk")
        return out

    def _h_apology(self, p, s):
        if p["patience"] < 60:
            p["patience"] = min(100.0, p["patience"] + 30)
            p["respect"] = min(100, p["respect"] + 1)
            return self.say(p, "forgive")
        return self.say(p, "apology_idle")

    def _h_order(self, p, s):
        lang = p["lang"] or "ru"
        ideas = self.persona.get("ORDER_IDEAS", ORDER_IDEAS)
        pool = ideas.get(lang) or ideas.get("ru") or ORDER_IDEAS[lang]
        return self.say(p, "next_order", order=random.choice(pool))

    def _h_movement(self, p, s):
        return self.say(p, "next_move")

    # шутки
    def _h_joke(self, p, s, category: str | None = None):
        lang = p["lang"] or "ru"
        all_pool = self.jokes[lang]
        cat = category or self._joke_category(s.last_text)
        pool = [j for j in all_pool if j.get("cat") == cat] if cat else all_pool
        if not pool and cat == "dirty":
            pool = [j for j in all_pool if j.get("cat") in ("dirty", "spicy", "flirt")]
        if not pool:
            pool = all_pool
        recent = set(p["joke_seen"][-max(1, len(pool) // 2):])
        weights = [max(0.05, p["joke_w"].get(j["cat"], 1.0)) * (0.1 if j["id"] in recent else 1.0) for j in pool]
        j = random.choices(pool, weights)[0]
        s.last_joke = j
        p["joke_seen"] = (p["joke_seen"] + [j["id"]])[-20:]
        body = j["setup"] + ("\n" + j["punch"] if j["punch"] else "")
        return f"{self.say(p, 'joke_intro')}\n{body}\n{self.say(p, 'joke_rate')}"

    def _rate_joke(self, p, s, delta, key):
        if not s.last_joke:
            return self.say(p, "no_joke_yet")
        cat = s.last_joke["cat"]
        p["joke_w"][cat] = min(4.0, max(0.1, p["joke_w"].get(cat, 1.0) + delta))
        return self.say(p, key)

    def _h_funny(self, p, s):
        p["respect"] = min(100, p["respect"] + 1)
        return self._rate_joke(p, s, +0.5, "funny")

    def _h_lame(self, p, s):
        return self._rate_joke(p, s, -0.5, "lame")

    # ---------- РПС с моделью противника ----------
    def _h_rps(self, p, s):
        s.state = {"k": "rps"}
        return self.say(p, "rps_start")

    def _rps_predict(self, p: dict) -> str | None:
        hist, model = p["rps_last"], p["rps_model"]
        for order, table in ((2, model["o2"]), (1, model["o1"])):
            if len(hist) >= order:
                c = table.get("|".join(hist[-order:]))
                if c and sum(c.values()) >= 2:
                    return max(c, key=c.get)
        return None

    def _rps_learn(self, p: dict, move: str) -> None:
        hist, model = p["rps_last"], p["rps_model"]
        if len(hist) >= 1:
            model["o1"].setdefault(hist[-1], {}).setdefault(move, 0)
            model["o1"][hist[-1]][move] += 1
        if len(hist) >= 2:
            key = "|".join(hist[-2:])
            model["o2"].setdefault(key, {}).setdefault(move, 0)
            model["o2"][key][move] += 1
        hist.append(move)
        del hist[:-10]

    def _rps_round(self, p: dict, s: Session, move: str) -> str:
        st = p["stats"]["rps"]
        predicted = self._rps_predict(p)
        j_move = COUNTER[predicted] if predicted and random.random() > 0.12 else random.choice(MOVES)
        st["rounds"] += 1
        st["hits"] += int(predicted == move)
        self._rps_learn(p, move)
        names = MOVE_NAMES[p["lang"] or "ru"]
        lines = [self.say(p, "rps_pick", move=names[j_move])]
        if move == j_move:
            st["d"] += 1
            lines.append(self.say(p, "rps_draw"))
        elif BEATS[move] == j_move:
            st["w"] += 1
            p["respect"] = min(100, p["respect"] + 1)
            lines.append(self.say(p, "rps_user_wins"))
        else:
            st["l"] += 1
            lines.append(self.say(p, "rps_j_wins"))
        lines.append(self.say(p, "rps_more"))
        return " ".join(lines)

    # ---------- Угадай число с адаптивной сложностью ----------
    def _h_guess(self, p, s):
        n = p["guess_n"]
        k = max(3, math.ceil(math.log2(n)))
        s.state = {"k": "guess", "n": n, "t": random.randint(1, n), "tries": 0, "max": k}
        return self.say(p, "guess_start", n=n, k=k)

    def _guess_step(self, p: dict, s: Session, key: str) -> str:
        st, g = s.state, p["stats"]["guess"]
        if not re.fullmatch(r"\d+", key):
            return self.say(p, "guess_bad", n=st["n"])
        guess = int(key)
        st["tries"] += 1
        if guess == st["t"]:
            s.state = None
            g["w"] += 1
            if st["tries"] <= max(2, st["max"] - 2):  # слишком легко — расширяем диапазон
                p["guess_n"] = min(100, int(p["guess_n"] * 1.5))
            return self.say(p, "guess_win", t=st["t"], tries=st["tries"])
        if st["tries"] >= st["max"]:
            s.state = None
            g["l"] += 1
            p["guess_n"] = max(10, int(p["guess_n"] * 0.75))  # слишком сложно — сужаем
            return self.say(p, "guess_lose", t=st["t"])
        left = st["max"] - st["tries"]
        return self.say(p, "guess_up" if guess < st["t"] else "guess_down", left=left)

    # ---------- Викторина с упором на слабые места ----------
    def _quiz_bank(self, p: dict) -> dict[str, dict]:
        lang = p["lang"] or "ru"
        quiz_items = self.persona.get("QUIZ", QUIZ)
        bank = {}
        for q in quiz_items:
            if not isinstance(q, dict) or "id" not in q:
                continue
            q_text = q.get(lang) or q.get("ru") or q.get("en")
            ans = q.get("a")
            if q_text and isinstance(ans, list):
                bank[q["id"]] = {"q": q_text, "a": [norm(str(a)) for a in ans]}
        for c in p["custom_q"]:
            bank[c["id"]] = {"q": c["q"], "a": c["a"], "shown": c.get("shown")}
        return bank

    def _h_quiz(self, p, s):
        bank = self._quiz_bank(p)
        ids = list(bank)
        chosen: list[str] = []
        for _ in range(min(3, len(ids))):
            pool = [i for i in ids if i not in chosen]
            chosen.append(random.choices(pool, [p["quiz_w"].get(i, 1.0) for i in pool])[0])
        s.state = {"k": "quiz", "ids": chosen, "i": 0, "score": 0}
        return f"{self.say(p, 'quiz_start')}\n1/{len(chosen)}: {bank[chosen[0]]['q']}"

    def _quiz_step(self, p: dict, s: Session, key: str) -> str:
        st, bank = s.state, self._quiz_bank(p)
        qid = st["ids"][st["i"]]
        item = bank.get(qid, {"q": "?", "a": []})
        ok = any(key == a or a in key.split() or (len(a) > 3 and a in key) for a in item["a"])
        w = p["quiz_w"].get(qid, 1.0)
        qs = p["stats"]["quiz"]
        if ok:
            st["score"] += 1
            qs["right"] += 1
            p["quiz_w"][qid] = max(0.3, w * 0.7)
            out = self.say(p, "quiz_right")
        else:
            qs["wrong"] += 1
            p["quiz_w"][qid] = min(6.0, w * 1.6)
            out = self.say(p, "quiz_wrong", a=item.get("shown") or (item["a"][0] if item["a"] else "?"))
        st["i"] += 1
        if st["i"] >= len(st["ids"]):
            s.state = None
            return out + "\n" + self.say(p, "quiz_end", s=st["score"], n=len(st["ids"]))
        nxt = bank.get(st["ids"][st["i"]], {"q": "?"})
        return f"{out}\n{st['i'] + 1}/{len(st['ids'])}: {nxt['q']}"

    # ---------- Статистика ----------
    def _h_stats(self, p, s):
        r, g, q = p["stats"]["rps"], p["stats"]["guess"], p["stats"]["quiz"]
        acc = int(100 * r["hits"] / r["rounds"]) if r["rounds"] else 0
        if (p["lang"] or "ru") == "ru":
            return (f"Личное дело: {p['name']}\n"
                    f"Сообщений: {p['seen']} | Терпение: {max(0, int(p['patience']))}% | Уважение: {int(p['respect'])}%\n"
                    f"РПС: {r['w']} побед / {r['l']} поражений / {r['d']} ничьих; "
                    f"мои прогнозы твоих ходов: {acc}% ({r['rounds']} раундов)\n"
                    f"Угадай число: {g['w']} побед / {g['l']} поражений, текущий диапазон 1–{p['guess_n']}\n"
                    f"Викторина: {q['right']} верно / {q['wrong']} неверно\n"
                    f"Выученных фраз: {len(p['taught'])}")
        return (f"File: {p['name']}\n"
                f"Messages: {p['seen']} | Patience: {max(0, int(p['patience']))}% | Respect: {int(p['respect'])}%\n"
                f"RPS: {r['w']} W / {r['l']} L / {r['d']} D; my prediction accuracy: {acc}% ({r['rounds']} rounds)\n"
                f"Guess: {g['w']} W / {g['l']} L, current range 1–{p['guess_n']}\n"
                f"Quiz: {q['right']} right / {q['wrong']} wrong\n"
                f"Learned phrases: {len(p['taught'])}")


if PERSONA_FILE.is_file():
    load_persona()


# --------------------------------------------------------------------------
# Консольный интерфейс
# --------------------------------------------------------------------------
def main() -> None:
    for stream in (sys.stdout, sys.stdin):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    brain = JBrain()
    uid = "console"
    p = brain.profile(uid)
    if not p["lang"]:
        answer = input("Выберите язык / Choose language (ru/en): ").strip().lower()
        p["lang"] = "en" if answer.startswith("en") else "ru"
        brain.save()
    prompt = "Ты: " if p["lang"] == "ru" else "You: "
    print("J:", brain.opening(uid))
    session = brain.session(uid)
    while not session.done:
        try:
            line = input(prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            print("J:", brain.respond(uid, "bye"))
            break
        reply = brain.respond(uid, line)
        if reply:
            print("J:", reply)


if __name__ == "__main__":
    main()
