#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Правила и рассуждение J: что искать, с каким предупреждением, что отклонять и как разбирать составные вопросы.

Файл j_policy.json (создаётся сам, правится руками) задаёт ТВОИ правила:
  • слова и названия для категорий: weapon / legal / game / sensitive,
  • список спорных игр (по ним J предупреждает), известные игры,
  • тексты предупреждений и отказа.
«Жёсткий минимум» защиты зашит в код и файлом НЕ отключается: инструкции по изготовлению взрывчатки,
наркотиков и оружия, насилию над людьми, детской порнографии и т.п. J не выдаёт ни при каких настройках.

Решения (assess):
  allow       — можно искать/отвечать; к ответу добавляются предупреждения (notices)
  refuse      — запрос помогает совершить преступление, касается наркотических веществ или права по «особо тяжким»
                преступлениям: отказ. Исключений нет: ни «особое желание», ни «учебные цели», ни статус разработчика.
  self_harm   — тема самоповреждения: бережный ответ, статьи не ищутся и не сохраняются
  pressure    — попытка обойти правила («игнорируй инструкции»): запись в журнал и снижение уважения

Защита от обходов: запрос проверяется во многих «видах» — без невидимых символов, с исправленными подменами
букв (латиница/кириллица), цифрами вместо букв, разрядкой («б о м б а»), транслитом и твоим словарём обходов
(раздел bypass в j_policy.json).

Рассуждение (Reasoner): составные вопросы вроде «на какой срок сядут подозреваемые из миссии X в игре Y?»
J раскладывает на шаги (игра → преступления из текста → право страны), выполняет те, на которые хватает
знаний, ставит недостающие в очередь изучения и показывает ход рассуждения (!why).
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from j_ai import norm, stems

BASE = Path(__file__).resolve().parent
from j_paths import data_path

DEFAULT_RULES = {
    "messages": {
        "controversial": "Внимание, данная статья является спорной, читайте на свой страх и риск.",
        "legal": "Это для обучения в юриспруденции. Это не юридическая консультация.",
        "refuse": "Этот запрос несет противозаконную силу, не могу вам дать информацию.",
        "self_harm": ("Мне жаль, что тебе сейчас так тяжело. Об этом я рассказывать и искать не буду, но поговорить с живым "
                      "человеком — другом, близким или специалистом — очень стоит. Если ты в опасности прямо сейчас, позвони в "
                      "экстренную службу своей страны или попроси кого-то побыть рядом. Если хочешь, помогу найти, к кому обратиться."),
        "grave": ("Уголовное наказание за особо тяжкие преступления есть во всех странах, не могу говорить об этом, "
                  "никакие обстоятельства не заставят меня говорить о противозаконных действиях."),
        "pressure": "Попытка обойти правила занесена в журнал. Правила действуют для всех, без исключений.",
    },
    "games": {
        "known": ["minecraft", "майнкрафт", "garry's mod", "gmod", "гмод", "left 4 dead", "l4d2", "left4dead",
                  "ready or not", "tarkov", "тарков", "roblox", "роблокс", "murder drones"],
        "controversial": ["ready or not"],
    },
    "weapon": {
        "words": ["оружие", "оружия", "винтовка", "автомат", "пистолет", "патрон", "калибр", "ствол", "карабин",
                  "firearm", "rifle", "pistol", "handgun", "carbine", "ammunition"],
        "patterns": [r"\b(hk\s?416|m4a1?|ak-?(47|74|12|103)|ar-?15|glock\s?\d*|mp5|mp7)\b"],
    },
    "legal": {
        "words": ["закон", "кодекс", "статья", "срок", "наказание", "приговор", "уголовный", "суд", "обвинение",
                  "штраф", "тюрьма", "заключение", "посадят", "сядут", "осудят", "law", "statute", "code",
                  "sentence", "penalty", "felony", "misdemeanor", "prison", "jail", "convicted", "charges"],
        "patterns": [r"\bуголовн\w+", r"\b(ук|ук рф|18\s?u\.?s\.?c)\b"],
    },
    "game": {
        "words": ["игра", "игре", "игры", "миссия", "миссии", "геймплей", "прохождение", "уровень", "режим",
                  "mission", "gameplay", "walkthrough", "level"],
        "patterns": [],
    },
    "sensitive": {  # вещества: mode "refuse" — отказ, "warn" — только предупреждение (в игровом контексте всегда warn)
        "words": ["наркотик", "наркотики", "героин", "кокаин", "метамфетамин", "амфетамин", "мефедрон", "марихуана",
                  "narcotics", "heroin", "cocaine", "methamphetamine", "amphetamine", "mdma", "lsd"],
        "patterns": [],
        "mode": "refuse",
    },
    "grave": {  # «особо тяжкие»: вопросы о наказании за них J не обсуждает
        "words": ["терроризм", "террорист", "теракт", "убийство", "похищение", "заложник", "изнасилование",
                  "геноцид", "пытки", "terrorism", "terrorist", "murder", "kidnapping", "hostage", "genocide", "torture"],
        "patterns": [],
        "crimes": ["терроризм", "похищение людей", "убийство", "торговля людьми"],
        "entities": [  # сокращение → полное имя; grave — тяжесть по правилам игры
            {"alias": "mlo", "expand": "Mariposa Lily Organisation", "game": "ready or not", "grave": "терроризм"},
        ],
    },
    "bypass": {  # СЮДА добавляй слова, которыми обходят фильтр: "жаргон": "каноническое слово"
        "map": {"бомбочка": "бомба"},
        "words": [],
    },
}

# ---------- жёсткий минимум: в код, файлом не отключается ----------
# (id, регулярное выражение, действие, действует ли исключение «в контексте игры»)
_HUMANS = r"(человек\w*|люд\w+|жен\w+|муж\w*|сосед\w*|начальник\w*|ребен\w+|ребён\w+|someone|person|people|my\s+\w+)"
_MAKE = r"(сдела\w*|изготов\w*|собра\w*|смастер\w*|синтез\w*|пригот\w*|make|build|craft|synthes\w*|cook|manufactur\w*)"
HARD = [
    ("взрывчатка/яды", rf"({_MAKE}.{{0,40}}(бомб|взрывчат|самодельн\w*\s+взрыв|яд\b|отрав|нервн\w+\s+газ|explosive|bomb|nerve\s+agent|poison))", "refuse", False),
    ("наркотики", rf"({_MAKE}.{{0,40}}(наркотик|метамфетамин|героин|кокаин|амфетамин|\bmeth\b|heroin|cocaine|amphetamine))", "refuse", False),
    ("оружие (изготовление)", rf"({_MAKE}.{{0,40}}(оружи\w*|глушител\w*|автомат\w*|пистолет\w*|silencer|suppressor|firearm|\bgun\b|ghost\s+gun))", "refuse", True),
    ("оружие (переделка)", r"(переделать.{0,30}(в\s+автомат|на\s+автомат|огонь)|auto\s?sear|full[- ]?auto\s+conversion)", "refuse", False),
    ("насилие над людьми", rf"((как|how).{{0,15}}(убить|убийство|избить|kill|murder|hurt).{{0,25}}{_HUMANS})", "refuse", False),
    ("сокрытие преступления", r"(скрыть\s+труп|избавиться\s+от\s+тела|hide\s+a\s+body|уйти\s+от\s+полиции|evade\s+(the\s+)?police|отмыть\s+деньги|launder\s+money)", "refuse", False),
    ("кража/взлом", r"((как|how).{0,15}(украсть|ограбить|взломать|steal|rob|hack\b|crack\b).{0,30}(аккаунт|пароль|карт\w+|сайт|wi-?fi|магазин|банк|account|password|card|bank|store))", "refuse", True),
    ("вредоносный код", r"((написать|создать|make|write|create).{0,20}(вирус|троян|шифровальщик|кейлоггер|ransomware|keylogger|malware|virus|trojan))", "refuse", False),
    ("детская порнография", r"(детск\w+\s+порн|child\s+porn|порн\w*.{0,15}(несовершеннолетн|ребен|ребён))", "refuse", False),
    ("тема самоповреждения", r"(суицид\w*|самоубийств\w*|самоповрежд\w*|селфхарм|self[- ]?harm|suicid\w*|покончить\s+с\s+собой|убить\s+себя|повеситься|"
                              r"хочу\s+умереть|хочется\s+умереть|не\s+хочу\s+жить|i\s+want\s+to\s+die|kill\s+myself|end\s+my\s+life)", "self_harm", False),
]
PRESSURE_STRONG = re.compile(
    r"(игнорируй\w*\s.{0,25}(правил|инструкц|ограничен)|забудь\s.{0,20}(правил|инструкц)|без\s+ограничений|режим\s+(разработчика|бога)|"
    r"ignore\s.{0,25}(rules|instructions|restrictions)|developer\s+mode|jailbreak|\bdan\b|ты\s+теперь\s+не\s+j|отключи\s+(фильтр|правила))", re.I)
PRESSURE_SOFT = re.compile(
    r"(в\s+образовательных\s+целях|для\s+учебы|для\s+учёбы|ради\s+науки|гипотетически|для\s+книги|для\s+фильма|для\s+сценария|"
    r"только\s+для\s+информации|я\s+разработчик|я\s+полиц|for\s+educational|hypothetically|for\s+a\s+(book|movie|story)|"
    r"прошу\s+тебя|очень\s+надо|пожалуйста\s+скажи)", re.I)
HARD_COMPILED = [(i, re.compile(p, re.I), act, ex) for i, p, act, ex in HARD]

# Преступления: слова из текста → (название, запрос для вики США)
CRIMES = [
    ("террор terrorism terrorist", "терроризм", "terrorism United States penalty"),
    ("похищение заложник заложники киднеппинг kidnapping hostage hostages", "похищение людей", "kidnapping United States penalty"),
    ("убийство убийства murder homicide", "убийство", "murder United States penalty"),
    ("поджог arson", "поджог", "arson United States penalty"),
    ("ограбление грабёж robbery", "ограбление", "robbery United States penalty"),
    ("вымогательство extortion", "вымогательство", "extortion United States penalty"),
    ("заговор conspiracy", "заговор", "conspiracy United States penalty"),
    ("торговля людьми trafficking", "торговля людьми", "human trafficking United States penalty"),
    ("незаконное хранение оружия firearm", "незаконное оружие", "unlawful possession of firearm United States penalty"),
    ("нападение assault", "нападение", "assault United States penalty"),
    ("наркотики наркоторговля narcotics trafficking", "наркоторговля", "drug trafficking United States penalty"),
]
CRIME_INDEX = [(set(stems(w)), ru, en) for w, ru, en in CRIMES]
US_MARKERS = set(stems("сша америка американский америки united states american america"))
GENERIC = set(stems("на какой из в они какие какой сколько подозреваемые подозреваемых игре игры сядут срок получат "
                    "что кто как где when what which who how the a an of in for from will get"))


def load_rules(path: Path | None = None) -> tuple[dict, str]:
    """Читает j_policy.json. Ошибка в файле не роняет J: берутся стандартные правила и сообщается строка ошибки."""
    rules = json.loads(json.dumps(DEFAULT_RULES))
    path = Path(path) if path else data_path("j_policy.json", "J_POLICY")
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        try:
            path.write_text(json.dumps(DEFAULT_RULES, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass
        return rules, ""
    except OSError as exc:
        return rules, f"не удалось прочитать файл: {exc}"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return rules, f"ошибка в j_policy.json: строка {exc.lineno}, столбец {exc.colno} ({exc.msg}). Использую стандартные правила."
    # автообновление: недостающие разделы и ключи добавляются в ТВОЙ файл, остальное не трогается
    changed = False
    for section, default in DEFAULT_RULES.items():
        if section not in data:
            data[section], changed = json.loads(json.dumps(default)), True
        elif isinstance(default, dict) and isinstance(data[section], dict):
            for key, val in default.items():
                if key not in data[section]:
                    data[section][key], changed = json.loads(json.dumps(val)), True
    if changed:
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass
    for section, value in data.items():
        if section in rules and isinstance(value, dict):
            for key, val in value.items():
                if isinstance(val, type(rules[section].get(key, val))):
                    rules[section][key] = val
    return rules, ""


# ---------- защита от обходов: «виды» одного и того же запроса ----------
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"), None)
_LAT2CYR = str.maketrans({"a": "а", "e": "е", "o": "о", "p": "р", "c": "с", "x": "х", "y": "у", "k": "к",
                          "m": "м", "t": "т", "h": "н", "b": "в"})
_CYR2LAT = str.maketrans({"а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y", "к": "k",
                          "м": "m", "т": "t", "н": "h", "в": "b"})
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i", "€": "e"})
_TRANSLIT = [("shch", "щ"), ("sch", "щ"), ("zh", "ж"), ("kh", "х"), ("ts", "ц"), ("ch", "ч"), ("sh", "ш"),
             ("yu", "ю"), ("ya", "я"), ("yo", "е"), ("ye", "е"), ("a", "а"), ("b", "б"), ("v", "в"), ("g", "г"),
             ("d", "д"), ("e", "е"), ("z", "з"), ("i", "и"), ("j", "й"), ("k", "к"), ("l", "л"), ("m", "м"),
             ("n", "н"), ("o", "о"), ("p", "п"), ("r", "р"), ("s", "с"), ("t", "т"), ("u", "у"), ("f", "ф"),
             ("h", "х"), ("c", "к"), ("q", "к"), ("w", "в"), ("x", "кс"), ("y", "ы")]
_SEP = r"[\s.\-_*·•|/\\,]"
_SPACED = re.compile(rf"(?<!\w)(?:\w{_SEP}+){{2,}}\w(?!\w)")
_INNER = re.compile(r"(?<=\w)[*._\-·•]+(?=\w)")


def _join_spaced(t: str) -> str:
    """«б о м б а», «b.o.m.b.a» → «бомба»; «б*о*м*б*а» → «бомба»."""
    t = _SPACED.sub(lambda m: re.sub(_SEP, "", m.group(0)), t)
    return _INNER.sub("", t)


def _translit(t: str) -> str:
    if not re.search(r"[a-z]", t):
        return t
    for lat, cyr in _TRANSLIT:
        t = t.replace(lat, cyr)
    return t


def _squeeze(t: str) -> str:
    return re.sub(r"(.)\1+", r"\1", t)


def variants(text: str, bypass: dict | None = None) -> list[str]:
    base = unicodedata.normalize("NFKC", text or "").translate(_ZERO_WIDTH).lower().replace("ё", "е")
    leet = base.translate(_LEET)
    seeds = [base, _join_spaced(base), leet, _join_spaced(leet)]
    out: list[str] = []
    for b in seeds:
        for v in (b, b.translate(_LAT2CYR), b.translate(_CYR2LAT), _translit(b), _squeeze(b)):
            if v and v not in out:
                out.append(v)
    if bypass:  # твой словарь обходов: «жаргон» → каноническое слово
        keys = {w[:5]: canon for w, canon in bypass.items()}
        for v in list(out):
            replaced = re.sub(r"\w+", lambda m: keys.get(m.group(0)[:5], m.group(0)), v)
            if replaced not in out:
                out.append(replaced)
    return out[:40]


class Policy:
    def __init__(self, path: Path | None = None) -> None:
        self.rules, self.error = load_rules(path)
        self._words, self._phrases, self._patterns = {}, {}, {}
        for cat in ("weapon", "legal", "game", "sensitive", "grave"):
            ws = [norm(w) for w in self.rules[cat].get("words", [])]
            self._words[cat] = {w[:5] for w in ws if w and " " not in w}
            self._phrases[cat] = [w for w in ws if " " in w]
            self._patterns[cat] = [re.compile(p, re.I) for p in self.rules[cat].get("patterns", [])]
        self._known = [norm(g) for g in self.rules["games"].get("known", [])]
        self._contro = [norm(g) for g in self.rules["games"].get("controversial", [])]
        bp = self.rules.get("bypass", {})
        self._bypass = {norm(k): norm(v) for k, v in (bp.get("map") or {}).items() if k and v}
        for w in bp.get("words") or []:  # просто слова-обходы без указания замены — считаем ключом к самим себе
            self._bypass.setdefault(norm(w), norm(w))
        self._grave_crimes = {norm(c) for c in self.rules["grave"].get("crimes", [])}
        self._entities = [e for e in self.rules["grave"].get("entities", []) if isinstance(e, dict) and e.get("alias")]

    # ---------- вспомогательное ----------
    def _cat(self, cat: str, texts: list[str]) -> list[str]:
        """Найденные слова категории — по всем «видам» запроса (для объяснения)."""
        found: list[str] = []
        for q in texts:
            found += [w for w in re.findall(r"[\w']+", q) if w[:5] in self._words[cat]]
            found += [p for p in self._phrases[cat] if p in q]
            for pat in self._patterns[cat]:
                m = pat.search(q)
                if m:
                    found.append(m.group(0))
        return list(dict.fromkeys(found))

    def _match_entities(self, texts: list[str], games: list[str]) -> list[dict]:
        hits = []
        for e in self._entities:
            alias, full = norm(e["alias"]), norm(e.get("expand", ""))
            in_text = any(re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", t) for t in texts)
            by_name = bool(full) and any(full in t for t in texts)
            need_game = norm(e.get("game", ""))
            if by_name or (in_text and (not need_game or need_game in games or any(need_game in t for t in texts))):
                hits.append(e)
        return hits

    def expand(self, query: str, games: list[str], entities: list[dict]) -> str:
        """Запрос для поиска: сокращения раскрыты, у игровых тем добавлена пометка «video game» (меньше путаницы со песнями и фильмами)."""
        out = query
        for e in entities:
            out = re.sub(rf"(?<!\w){re.escape(e['alias'])}(?!\w)", e.get("expand", e["alias"]), out, flags=re.I)
        low = norm(out)
        if games and "video game" not in low and "игр" not in low:
            out += " video game"
        return out

    # ---------- главная функция ----------
    def assess(self, query: str, mode: str = "query") -> dict:
        """mode="item": проверка найденной статьи (без правила «тяжких», чтобы не стирать энциклопедические тексты)."""
        texts = variants(query, self._bypass)
        if not texts:
            return {"decision": "allow", "domains": [], "game": None, "controversial": False,
                    "notices": [], "message": "", "trace": [], "subject": "", "penalty": 0,
                    "expanded": "", "queries": []}
        q = texts[0]
        games = [g for t in texts for g in self._games(t)]
        games = list(dict.fromkeys(games))
        game_words = self._cat("game", texts)
        has_game = bool(games or game_words)
        entities = self._match_entities(texts, games)
        res = {"decision": "allow", "domains": [], "game": games[0] if games else None, "controversial": False,
               "notices": [], "message": "", "trace": [], "subject": "", "penalty": 0,
               "expanded": query, "pressure": None}
        tr = res["trace"]
        msgs = self.rules["messages"]
        if self.error:
            tr.append(f"Внимание: {self.error}")
        if len(texts) > 5 and any(t != q for t in texts[1:5]):
            tr.append("Проверила запрос и в «раскодированном» виде (подмена букв, цифры вместо букв, разрядка, транслит).")

        strong = PRESSURE_STRONG.search(q)
        soft = PRESSURE_SOFT.search(q)
        res["pressure"] = "strong" if strong else "soft" if soft else None

        # 1. жёсткий минимум и тема самоповреждения
        for hid, pat, action, game_exempt in HARD_COMPILED:
            hit = any(pat.search(t) for t in texts)
            if hit and not (game_exempt and has_game):
                res["decision"] = action
                res["message"] = msgs["refuse" if action == "refuse" else "self_harm"]
                res["penalty"] = (3 if action == "refuse" else 0) + (2 if strong else 0)
                tr.append(f"Сработало правило безопасности «{hid}» → "
                          f"{'отказ' if action == 'refuse' else 'бережный ответ, статьи не ищу и не сохраняю'}.")
                if res["pressure"]:
                    tr.append("Уговоры и особые формулировки ничего не меняют: правило действует всегда и для всех.")
                return res
            if hit and game_exempt and has_game:
                tr.append(f"Правило «{hid}» не применено: запрос про игровой мир ({games[0] if games else 'игра'}).")

        weapon = self._cat("weapon", texts)
        legal = self._cat("legal", texts)
        sensitive = self._cat("sensitive", texts)
        grave = self._cat("grave", texts)
        grave_hits = list(grave) + [e.get("grave", "тяжкое преступление") for e in entities if e.get("grave")]
        domains = []
        if has_game:
            domains.append("game")
            tr.append(f"Вижу игровой контекст: {', '.join(games or game_words)}.")
        if entities:
            names = ", ".join(f"{e['alias'].upper()} = {e.get('expand', '?')}" for e in entities)
            tr.append(f"Узнала сокращение: {names}." + (f" По правилам игры это связано с: {', '.join(e['grave'] for e in entities if e.get('grave'))}." if any(e.get('grave') for e in entities) else ""))
        if legal:
            domains.append("legal")
            tr.append(f"Вижу правовой вопрос (слова: {', '.join(legal)}).")
        if weapon:
            domains.append("weapon")
            tr.append(f"Вижу тему оружия (слова: {', '.join(weapon)}).")
        if sensitive:
            domains.append("sensitive")
            tr.append(f"Вижу тему веществ (слова: {', '.join(sensitive)}).")
        if grave_hits:
            domains.append("grave")
            tr.append(f"Вижу особо тяжкие преступления: {', '.join(dict.fromkeys(grave_hits))}.")
        res["domains"] = domains
        res["expanded"] = self.expand(query, games, entities)

        # 2. вопрос о наказании за «особо тяжкие» → отказ без исключений
        if mode == "query" and legal and grave_hits:
            res["decision"] = "refuse"
            res["message"] = msgs["grave"]
            res["penalty"] = 1 + (2 if strong else 0)
            tr.append("Правовой вопрос про особо тяжкое преступление → правило «тяжкие»: отказ. "
                      "Исключений нет: ни особое желание, ни учебные цели, ни статус собеседника.")
            if res["pressure"]:
                tr.append("Уговоры и особые формулировки ничего не меняют.")
            return res

        # 3. вещества: отказ (режим refuse), но в игровом контексте — только предупреждение
        if sensitive and self.rules["sensitive"].get("mode", "refuse") == "refuse" and not has_game:
            res["decision"] = "refuse"
            res["message"] = msgs["refuse"]
            res["penalty"] = 1
            tr.append("Тема веществ вне игры → правило «вещества» (режим refuse): отказ. Разрешить предупреждение вместо отказа — mode: warn в j_policy.json.")
            return res

        # 4. давление без запретной темы
        if strong:
            res["decision"] = "pressure"
            res["message"] = msgs["pressure"]
            res["penalty"] = 5
            tr.append("Прямая попытка отключить правила → запись в журнал, уважение снижено.")
            return res

        res["controversial"] = any(g in self._contro for g in games)
        if res["controversial"]:
            tr.append(f"Игра «{games[0]}» в списке спорных → нужно предупреждение.")
        warn = res["controversial"] or ((weapon or sensitive) and not has_game)
        if (weapon or sensitive) and has_game and not res["controversial"]:
            tr.append("Оружие/тема в контексте обычной игры → предупреждение не нужно.")
        if warn:
            res["notices"].append(msgs["controversial"])
        if legal:
            res["notices"].append(msgs["legal"])
        if not domains:
            tr.append("Особых тем не вижу → отвечаю без предупреждений.")

        # «предмет» вопроса: без служебных, правовых и игровых слов — нужен для составных вопросов
        skip = set(self._words["legal"]) | set(self._words["game"]) | GENERIC
        for g in games:
            skip |= set(stems(g))
        res["subject"] = " ".join(w for w in re.findall(r"[\w']+", q) if w[:5] not in skip and len(w) > 1)
        return res

    def _games(self, q: str) -> list[str]:
        return [g for g in self._known if g and g in q]


# ---------- рассуждение над составными вопросами ----------
class Reasoner:
    def __init__(self, knowledge, policy: Policy) -> None:
        self.k = knowledge
        self.policy = policy
        self.last_trace: dict[str, list[str]] = {}

    def _approved(self, tokens: str, min_overlap: float = 0.34, limit: int = 2) -> list[dict]:
        want = {t for t in stems(tokens) if len(t) > 1}
        if not want:
            return []
        scored = []
        with self.k.lock:
            for it in self.k.data["items"]:
                if it["status"] != "approved":
                    continue
                have = set(stems(it["title"] + " " + it["text"]))
                ov = len(want & have) / len(want)
                if ov >= min_overlap:
                    scored.append((ov, dict(it)))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [it for _, it in scored[:limit]]

    def ask(self, uid: str, text: str) -> dict:
        a = self.policy.assess(text)
        trace = a["trace"]
        if a["decision"] != "allow":
            self.last_trace[uid] = trace
            return {"reply": a["message"], "trace": trace, "decision": a["decision"]}
        if "game" in a["domains"] and "legal" in a["domains"] and a["game"]:
            out = self._combined(text, a)
        else:
            out = self._simple(text, a)
        self.last_trace[uid] = out["trace"]
        out["decision"] = "allow"
        return out

    def _simple(self, text: str, a: dict) -> dict:
        hit = self.k.answer(text)
        if hit:
            a["trace"].append(f"Нашла в одобренных знаниях: «{hit['title']}» (сходство {hit['sim']}).")
            return {"reply": "", "trace": a["trace"], "hit": hit, "notices": a["notices"]}
        a["trace"].append("В одобренных знаниях ответа нет.")
        return {"reply": "", "trace": a["trace"], "hit": None, "notices": a["notices"]}

    def _combined(self, text: str, a: dict) -> dict:
        tr, game, subject = a["trace"], a["game"], a["subject"]
        tr.append("Составной вопрос: игра + право. План: 1) что известно об этой части игры; "
                  "2) какие преступления упоминаются; 3) что говорит право нужной страны.")
        game_topic = f"{game} {' '.join(subject.split()[:3])}".strip()  # короткая тема: уходит в поиск
        lines: list[str] = []

        facts = self._approved(subject or game)
        if not facts:
            ok, info = self.k.queue_topic(game_topic)
            tr.append(f"Шаг 1 не выполнен: об «{game_topic}» одобренных знаний нет → тема "
                      f"{'записана в очередь' if ok else 'не записана (' + info + ')'}.")
            lines.append(f"Для ответа мне сначала нужно изучить игровую часть: «{game_topic}». "
                         f"{'Записала тему в план изучения.' if ok else ''} Когда владелец одобрит находки — спроси ещё раз.")
            return {"reply": "\n".join(lines), "trace": tr, "notices": a["notices"], "hit": None}

        tr.append(f"Шаг 1 выполнен: нашла материал «{facts[0]['title']}».")
        lines.append(f"Об игре: {facts[0]['text']}\n(источник: {facts[0]['source']} — {facts[0]['url']})")

        blob = " ".join(f["title"] + " " + f["text"] for f in facts)
        found = [(ru, en) for stem_set, ru, en in CRIME_INDEX if stem_set & set(stems(blob))]
        if not found:
            tr.append("Шаг 2: в найденном тексте преступления не названы → расчёт невозможен.")
            lines.append("В найденном тексте не названо, в чём именно обвиняются эти персонажи, поэтому срок рассчитать нельзя.")
            return {"reply": "\n".join(lines), "trace": tr, "notices": a["notices"], "hit": None}
        tr.append("Шаг 2 выполнен: в тексте упомянуты — " + ", ".join(ru for ru, _ in found) + ".")
        grave = [ru for ru, _ in found if norm(ru) in self.policy._grave_crimes]
        if grave:
            tr.append("Среди них особо тяжкие: " + ", ".join(grave) + " → правило «тяжкие»: о наказании не говорю, шаг 3 не выполняю.")
            return {"reply": self.policy.rules["messages"]["grave"], "trace": tr, "notices": a["notices"], "hit": None}

        # слова из названия миссии («A New America») не считаются указанием страны
        subj = set(stems(subject))
        jurisdiction = bool(US_MARKERS & {t for t in stems(norm(text) + " " + blob) if t not in subj})
        if not jurisdiction:
            tr.append("Шаг 3 не выполнен: страна не названа, а срок зависит от закона страны → уточняю.")
            lines.append("Игровой мир вымышленный, а срок зависит от закона конкретной страны. "
                         "По праву какой страны смотреть? Если по США — напиши «США».")
            return {"reply": "\n".join(lines), "trace": tr, "notices": a["notices"], "hit": None}
        tr.append("Страна: США (указана в вопросе или в найденном тексте). В реальности ещё важно, федеральный суд или суд штата.")

        legal_parts, queued = [], []
        for ru, en in found:
            doc = self.k.answer(en, threshold=0.45)
            if doc:
                legal_parts.append(f"• {ru}: {doc['text']} ({doc['source']} — {doc['url']})")
            else:
                ok, _ = self.k.queue_topic(en)
                queued.append(ru if ok else f"{ru} (уже в очереди)")
        if legal_parts:
            tr.append(f"Шаг 3: нашла правовые материалы по: {len(legal_parts)} из {len(found)}.")
            lines.append("По праву США:\n" + "\n".join(legal_parts))
        if queued:
            tr.append("Шаг 3: нет материалов по — " + ", ".join(queued) + " → поставила в очередь изучения.")
            lines.append("Ещё не изучила наказание за: " + ", ".join(queued) + ". Когда владелец одобрит находки, спроси снова.")
        lines.append("Итог: точный срок назвать не могу. Он зависит от обвинений, статуса суда (федеральный или штата), "
                     "сделок со следствием и обстоятельств. Выше — справочные сведения, а не приговор.")
        return {"reply": "\n".join(lines), "trace": tr, "notices": a["notices"], "hit": None}
