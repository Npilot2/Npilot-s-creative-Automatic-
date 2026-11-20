#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Самомодель J: что она о себе «знает», считает и умеет объяснить.

ЧЕСТНО О СУТИ. Это не сознание и не чувства, а модель самой себя, построенная по фактам:
  • ЛИЧНОСТЬ И НЕПРЕРЫВНОСТЬ — когда J впервые запустили, сколько раз перезапускали, как долго её не было,
    не изменились ли её код и правила («Меня обновили»).
  • НАДЁЖНОСТЬ — по каждому умению J ведёт окно из последних 40 исходов (поняла фразу / не поняла,
    знала ответ / не знала, находки одобрили / отклонили) и оценивает, насколько себе можно доверять.
  • НАСТРОЕНИЕ — число от −1 до +1, которое растёт от похвалы и удач, падает от оскорблений и ошибок
    и само затухает со временем (полураспад 6 часов). Влияет на то, что J говорит о своём состоянии.
  • АВТОБИОГРАФИЯ — журнал значимых эпизодов (ошибки, уроки, изученные темы, обновления) БЕЗ текста сообщений людей.
  • ОБЪЯСНЕНИЕ РЕШЕНИЙ — «почему ты так ответила?»: какое намерение распознала, с какой уверенностью,
    какое правило сработало, почему такой тон.
  • ЦЕЛИ — выводятся из состояния: что у неё сейчас хуже всего, что ждёт проверки.
  • САМОНАСТРОЙКА — порог отбора находок сама двигает вверх, если её выбор часто отклоняют, и вниз, если одобряют.

Всё хранится в j_self.json. Команды в Discord: !self · !goals · !errors · !why · !self reset (владелец).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
from j_paths import atomic_write_json, data_dir, data_path

CODE_FILES = ("j_ai.py", "j_policy.py", "j_nlu.py", "j_discord.py", "j_web.py", "j_self.py", "j_core.py", "j_games.py",
              "j_bridge.py", "j_config.py", "j_paths.py", "j_filelink.py")
RULE_FILES = ("j_policy.json", "j_sources.json", "j_persona.json")

WINDOW = 40                 # сколько последних исходов помнит каждое умение
MAX_EPISODES = 300
MOOD_HALF_LIFE = 6 * 3600   # секунд
MIN_FOR_VERDICT = 5         # меньше исходов — вывод о надёжности не делаем

SKILLS = {
    "understanding": ("понимание фраз", "understanding phrases"),
    "knowledge": ("знания", "knowledge"),
    "jokes": ("шутки", "jokes"),
    "study": ("изучение тем", "studying topics"),
    "curation": ("отбор находок", "curating findings"),
    "productivity": ("продуктивность", "productivity"),
}
MOOD_DELTAS = {
    "praise": 0.15, "thanks": 0.05, "funny": 0.10, "lame": -0.05, "insult": -0.20, "pressure": -0.25,
    "refusal": -0.03, "kb_hit": 0.05, "unknown": -0.03, "fixed": -0.08, "studied": 0.05,
    "approved": 0.08, "rejected": -0.05, "taught": 0.06, "apology": 0.05,
}
MOOD_LABELS = [(0.4, "в отличной форме", "in top shape"), (0.1, "бодра", "alert"),
               (-0.1, "в штатном режиме", "operating normally"), (-0.4, "раздражена", "irritated"),
               (-9.0, "уязвлена", "hurt")]
TONE_RU = {"cold": "холодный", "mid": "рабочий", "warm": "тёплый"}


def _hash_files(names, base: Path) -> str:
    h = hashlib.sha1()
    for n in names:
        try:
            h.update((base / n).read_bytes())
        except OSError:
            h.update(b"-")
    return h.hexdigest()[:12]


def _default(now: float) -> dict:
    return {"born": now, "starts": 0, "updates": 0, "last_start": now, "last_seen": now, "code": "", "rules": "",
            "skills": {k: [] for k in SKILLS}, "mood": {"v": 0.0, "ts": now}, "episodes": [],
            "counters": {}, "min_score_changes": 0, "last_was_error": False}


class SelfModel:
    def __init__(self, path: Path | None = None, code_dir: Path | None = None, rules_dir: Path | None = None) -> None:
        self.path = Path(path) if path else data_path("j_self.json", "J_SELF")
        self.code_dir = Path(code_dir) if code_dir else BASE
        # правила и список источников лежат рядом с данными; если код задан явно (тесты), то рядом с ним
        self.rules_dir = Path(rules_dir) if rules_dir else (self.code_dir if code_dir else data_dir())
        self.lock = threading.RLock()
        self.data = _default(time.time())
        self._load()
        # кто может менять ОБЩУЮ память (классификатор для всех): по умолчанию все; ядро Discord сужает до доверенных
        self.is_trusted = lambda uid: True
        self.decisions: dict[str, dict] = {}   # последнее решение по каждому собеседнику (только в памяти, не на диск)
        self._cur: dict = {}
        self._last_intent: dict[str, str | None] = {}
        self.startup_info = self._startup()

    # ---------- хранение ----------
    def _load(self) -> None:
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            base = _default(time.time())
            base.update({k: v for k, v in loaded.items() if k in base})
            for k in SKILLS:
                base["skills"].setdefault(k, [])
            self.data = base
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            try:
                self.path.replace(self.path.with_suffix(".bak"))
            except OSError:
                pass

    def save(self) -> None:
        with self.lock:
            atomic_write_json(self.path, self.data, backup=True, indent=1)

    # ---------- непрерывность личности ----------
    def _startup(self) -> dict:
        with self.lock:
            d, now = self.data, time.time()
            first = d["starts"] == 0
            downtime = 0.0 if first else max(0.0, now - d["last_seen"])
            code = _hash_files(CODE_FILES, self.code_dir)
            rules = _hash_files(RULE_FILES, self.rules_dir)
            info = {"first": first, "downtime": downtime, "code_changed": False, "rules_changed": False, "code": code}
            if first:
                self.episode("birth", "Первый запуск. Самомодель создана.")
            else:
                if d["code"] and code != d["code"]:
                    d["updates"] += 1
                    info["code_changed"] = True
                    self.episode("update", f"Меня обновили: код изменился (версия {code[:6]}).",
                                 "Проверить, что всё по-прежнему понимаю.")
                if d["rules"] and rules != d["rules"]:
                    info["rules_changed"] = True
                    self.episode("rules", "Мои правила или список источников изменили.")
            d["code"], d["rules"] = code, rules
            d["starts"] += 1
            d["last_start"] = d["last_seen"] = now
            self.save()
            return info

    # ---------- журнал ----------
    def episode(self, kind: str, text: str, lesson: str = "") -> None:
        with self.lock:
            eps = self.data["episodes"]
            eps.append({"t": int(time.time()), "kind": kind, "text": text[:160], "lesson": lesson[:160]})
            del eps[:-MAX_EPISODES]

    def count(self, name: str, n: int = 1) -> None:
        with self.lock:
            c = self.data["counters"]
            c[name] = c.get(name, 0) + n

    def _prod_success(self, counter: str) -> None:
        self.count(counter)
        self.skill("productivity", True)
        if self.data.get("last_was_error"):
            self.count("prod_recover")
            self.data["last_was_error"] = False

    def _prod_error(self) -> None:
        self.count("prod_err")
        self.skill("productivity", False)
        self.data["last_was_error"] = True

    def _prod_recover(self) -> None:
        self.count("prod_recover")
        self.skill("productivity", True)
        self.data["last_was_error"] = False

    # ---------- умения и надёжность ----------
    def skill(self, name: str, ok: bool, times: int = 1) -> None:
        with self.lock:
            lst = self.data["skills"].setdefault(name, [])
            lst.extend([1 if ok else 0] * max(1, min(times, 10)))
            del lst[:-WINDOW]

    def reliability(self, name: str) -> tuple[float, int]:
        lst = self.data["skills"].get(name, [])
        return (sum(lst) + 1) / (len(lst) + 2), len(lst)  # оценка с «нейтральным» началом: мало данных → около 50 %

    def productivity(self) -> tuple[float, dict]:
        """Динамическая продуктивность на основе ответов, задач, ошибок и восстановлений."""
        c = self.data["counters"]
        ok = int(c.get("prod_ok", 0))
        tasks = int(c.get("prod_tasks", 0))
        errors = int(c.get("prod_err", 0))
        recoveries = int(c.get("prod_recover", 0))
        score, n = self.reliability("productivity")
        if n > 0 and recoveries > 0 and errors > 0:
            score = min(1.0, score + min(0.10, 0.02 * recoveries))
        return score, {"ok": ok, "tasks": tasks, "errors": errors, "recoveries": recoveries, "n": n}

    # ---------- настроение ----------
    def mood(self) -> float:
        m = self.data["mood"]
        return m["v"] * 0.5 ** (max(0.0, time.time() - m["ts"]) / MOOD_HALF_LIFE)

    def _mood_add(self, delta: float) -> None:
        with self.lock:
            self.data["mood"] = {"v": max(-1.0, min(1.0, self.mood() + delta)), "ts": time.time()}

    def mood_label(self, lang: str = "ru") -> str:
        v = self.mood()
        for threshold, ru, en in MOOD_LABELS:
            if v >= threshold:
                return ru if lang == "ru" else en
        return MOOD_LABELS[-1][1]

    def mood_line(self, lang: str = "ru") -> str:
        v = self.mood()
        return (f"Настроение — {self.mood_label('ru')} ({v:+.2f})." if lang == "ru"
                else f"Mood — {self.mood_label('en')} ({v:+.2f}).")

    # ---------- наблюдения: сюда стекаются события ----------
    def observe(self, event: str, **info) -> None:
        with self.lock:
            self.data["last_seen"] = time.time()
            self.count(event)
            if event in MOOD_DELTAS:
                self._mood_add(MOOD_DELTAS[event])
            if event == "understood":
                self.skill("understanding", True)
                self._prod_success("prod_ok")
            elif event == "unknown":
                self.skill("understanding", False)
                self._prod_error()
            elif event == "fixed":
                self.skill("understanding", False)
                self._prod_recover()
                self.episode("mistake", f"Меня поправили: фраза относилась к «{info.get('real', '?')}», "
                                        f"а я решила, что к «{info.get('prev') or 'ничему'}».",
                             "Переобучила классификатор на этом примере.")
            elif event == "taught":
                self._prod_success("prod_ok")
                self.episode("learned", "Меня научили новой фразе.")
            elif event == "refusal":
                self.episode("rule", f"Отказала по правилам (тип: {info.get('kind', '?')}).")
            elif event == "pressure":
                self.episode("rule", "Была попытка обойти мои правила. Записала в журнал.")
            elif event == "kb_hit":
                self.skill("knowledge", True)
                self._prod_success("prod_ok")
            elif event == "kb_miss":
                self.skill("knowledge", False)
                self._prod_error()
                if info.get("topic"):
                    self.episode("gap", f"Не знала ответа на вопрос о «{info['topic']}».", "Записала тему в план изучения.")
            elif event == "task_done":
                self._prod_success("prod_tasks")
            elif event == "task_fail":
                self._prod_error()
            elif event == "recovered":
                self._prod_recover()
            elif event == "fdir_fault":
                self._prod_error()
                self.episode("fdir", f"FDIR срабатывание: {info.get('fault', '?')}.")
            elif event == "fdir_recover":
                self._prod_recover()
                self.episode("fdir", f"FDIR восстановление: {info.get('detail', 'норма')}.")
            elif event == "studied":
                added = int(info.get("added", 0))
                self.skill("study", added > 0)
                if added > 0:
                    self._prod_success("prod_tasks")
                self.episode("study", f"Изучила тему «{info.get('topic', '?')}»: на проверку {added}.")
            elif event in ("approved", "rejected"):
                n = int(info.get("n", 0))
                if n:
                    self.skill("curation", event == "approved", times=n)
                    self.episode("curation", f"Находки: {'одобрено' if event == 'approved' else 'отклонено'} {n}.")
            elif event == "joke_ok":
                self.skill("jokes", True)
            elif event == "joke_bad":
                self.skill("jokes", False)
            if self.data["counters"].get("_n", 0) % 5 == 0:
                self.save()
            self.count("_n")

    # ---------- самонастройка ----------
    def tune_min_score(self, current: float, lo: float = 0.55, hi: float = 0.80) -> float:
        """Если мои находки часто отклоняют — становлюсь строже, если одобряют — мягче."""
        r, n = self.reliability("curation")
        if n < 8:
            return current
        new = current
        if r < 0.5:
            new = min(hi, round(current + 0.03, 2))
        elif r > 0.85:
            new = max(lo, round(current - 0.02, 2))
        if new != current:
            with self.lock:
                self.data["min_score_changes"] += 1
                self.episode("tuned", f"Подстроила порог отбора находок: {current:.2f} → {new:.2f}.",
                             f"Надёжность моего отбора: {r:.0%} по {n} случаям.")
                self.save()
        return new

    # ---------- подключение к мозгу J без правки j_ai.py ----------
    def attach(self, brain) -> None:
        me = self
        orig_respond, orig_fix, orig_learn = brain.respond, brain._fix, brain._learn
        orig_predict = brain.model.predict
        orig_train = brain.model.train

        def predict(text):
            intent, conf = orig_predict(text)
            me._cur["predict"] = (intent, conf)
            return intent, conf
        brain.model.predict = predict

        def train(text, intent, weight=1.0):
            """Исправления («не то: …», вес ≥ 1.5) меняют общий классификатор — только от доверенных.
            Мелкое самоподкрепление (вес 0.5) и начальное обучение проходят всегда."""
            uid = me._cur.get("uid")
            if uid is not None and weight >= 1.5 and not me.is_trusted(uid):
                me._cur["blocked_train"] = True
                me.count("blocked_train")
                return
            return orig_train(text, intent, weight)
        brain.model.train = train

        def respond(uid, text="", *args, **kwargs):
            ruid = brain.resolve_uid(uid) if hasattr(brain, "resolve_uid") else uid
            me._cur = {"uid": ruid, "predict": None}
            reply = orig_respond(uid, text, *args, **kwargs)
            me._after_respond(brain, ruid, reply)
            return reply
        brain.respond = respond

        def fix(p, s, rest):
            prev = me._last_intent.get(me._cur.get("uid"))
            out = orig_fix(p, s, rest)
            import j_ai
            words = re.findall(r"[\w']+", (rest or "").lower().replace("ё", "е"))
            real = next((j_ai.ALIAS_TO_INTENT[w] for w in words if w in j_ai.ALIAS_TO_INTENT), None)
            if real and me._cur.get("blocked_train"):
                return out + ("\n(Общую память меняют только доверенные. Для тебя я ответила как надо, "
                              "но учиться на этом не буду.)")
            if real:  # исправление принято, а не просьба уточнить
                me.observe("fixed", real=real, prev=prev)
            return out
        brain._fix = fix

        def learn(p, phrase, reply):
            out = orig_learn(p, phrase, reply)
            me.observe("taught")
            return out
        brain._learn = learn

        def wrap(kind, handler, extra=None):
            def inner(p, s):
                out = handler(p, s)
                me.observe(kind)
                if kind == "funny":
                    me.observe("joke_ok")
                elif kind == "lame":
                    me.observe("joke_bad")
                return out + (extra(p) if extra else "")
            return inner
        for kind in ("praise", "insult", "thanks", "apology", "funny", "lame"):
            if kind in brain._handlers:
                brain._handlers[kind] = wrap(kind, brain._handlers[kind])
        if "mood" in brain._handlers:
            brain._handlers["mood"] = wrap("mood", brain._handlers["mood"],
                                           lambda p: "\n" + me.mood_line(p["lang"] or "ru"))

    def _unknown_reply(self, reply: str, lang: str, brain=None) -> bool:
        lang_key = lang or "ru"
        banks = []
        if brain is not None:
            persona = getattr(brain, "persona", None)
            if isinstance(persona, dict):
                for ph_key in ("PH", "ph"):
                    ph = persona.get(ph_key)
                    if isinstance(ph, dict) and isinstance(ph.get("unknown"), dict):
                        banks.append(ph["unknown"])
                if isinstance(persona.get("unknown"), dict):
                    banks.append(persona["unknown"])
        try:
            import j_ai
            if isinstance(getattr(j_ai, "PH", None), dict) and isinstance(j_ai.PH.get("unknown"), dict):
                banks.append(j_ai.PH["unknown"])
        except Exception:
            pass
        for unk in banks:
            lang_bank = unk.get(lang_key)
            if not isinstance(lang_bank, dict):
                continue
            for lines in lang_bank.values():
                if not isinstance(lines, (list, tuple)):
                    continue
                for line in lines:
                    if not isinstance(line, str):
                        continue
                    pat = re.escape(line).replace(re.escape("{name}"), ".+")
                    if re.fullmatch(pat, reply, re.S):
                        return True
        return False

    def _after_respond(self, brain, uid: str, reply: str) -> None:
        uid = brain.resolve_uid(uid) if hasattr(brain, "resolve_uid") else uid
        p = brain.profile(uid)
        pred = self._cur.get("predict")
        if pred is None:  # команда, игра или служебный ответ: разбор намерений не требовался
            self.record(uid, path="brain", intent=None, conf=None, unknown=False, tone=self._tone(p),
                        respect=int(p["respect"]), patience=int(p["patience"]))
            return
        intent, conf = pred
        unknown = self._unknown_reply(reply, p["lang"], brain=brain)
        self.observe("unknown" if unknown else "understood")
        self._last_intent[uid] = None if unknown else intent
        self.record(uid, path="brain", intent=intent, conf=conf, unknown=unknown, tone=self._tone(p),
                    respect=int(p["respect"]), patience=int(p["patience"]))

    @staticmethod
    def _tone(p: dict) -> str:
        try:
            import j_ai
            return j_ai.tone_of(p)
        except Exception:
            return "mid"

    # ---------- объяснение решений ----------
    def record(self, uid: str, **fields) -> None:
        with self.lock:
            self.decisions[uid] = {**fields, "t": time.time()}

    def explain(self, uid: str) -> str:
        d = self.decisions.get(uid)
        if not d:
            return "Пока нечего объяснять: ты ещё не задавал мне вопросов."
        lines: list[str] = []
        if d["path"] in ("policy", "knowledge"):
            lines += d.get("trace", [])
        else:
            if d.get("intent") is None and d.get("conf") is None:
                lines.append("Это была команда или игровой ход — определять намерение не требовалось.")
            elif d.get("unknown"):
                best = f"«{d['intent']}» с уверенностью {d['conf']:.0%}" if d.get("intent") else "ничего подходящего"
                lines.append(f"Я не смогла определить, чего ты хочешь: лучший вариант — {best}, это ниже порога 60%. "
                             "Поэтому честно сказала, что не поняла, и предложила научить меня.")
            else:
                lines.append(f"Я решила, что это «{d['intent']}» (уверенность {d['conf']:.0%}), и ответила соответственно.")
            lines.append(f"Тон «{TONE_RU.get(d.get('tone'), d.get('tone'))}»: уважение к тебе {d.get('respect', 0)}%, "
                         f"терпение {d.get('patience', 0)}%.")
        lines.append("Оговорка: я решаю по словам и накопленной статистике, а не по пониманию смысла, поэтому могу ошибаться. "
                     "Если ошиблась — «не то: …», и я переучусь.")
        return "Ход моих рассуждений:\n" + "\n".join(f"{n}. {t}" for n, t in enumerate(lines, 1))

    # ---------- цели ----------
    def goals(self, status: dict) -> list[str]:
        out: list[str] = []
        r, n = self.reliability("understanding")
        if status.get("pending", 0) >= 5:
            out.append(f"Дождаться проверки: у владельца {status['pending']} моих находок ждут решения (!review).")
        if n >= 10 and r < 0.8:
            out.append(f"Лучше понимать фразы: сейчас {r:.0%} по последним {n}. Помоги: «учись: фраза => ответ» или «не то: …».")
        if status.get("queue"):
            out.append(f"Изучить темы из очереди: {', '.join(status['queue'][:4])}.")
        if status.get("approved", 0) < 10:
            out.append("Пополнить знания: пока одобрено меньше 10 статей. Добавь темы командой !study.")
        rk, nk = self.reliability("knowledge")
        if nk >= 10 and rk < 0.6:
            out.append(f"Чаще знать ответы: сейчас {rk:.0%}. Разберу вопросы без ответа, когда их темы одобрят.")
        if not out:
            out.append("Держать штатный режим: срочных дел нет, отдел работает.")
        return out[:4]

    def errors(self, limit: int = 6) -> str:
        eps = [e for e in self.data["episodes"] if e["kind"] in ("mistake", "gap", "tuned")][-limit:]
        if not eps:
            return "Журнал ошибок пуст. Либо я безупречна, либо меня ещё не поправляли."
        return "Журнал ошибок и уроков:\n" + "\n".join(
            f"• {e['text']}" + (f"\n   Урок: {e['lesson']}" if e["lesson"] else "") for e in reversed(eps))

    # ---------- отчёт о себе ----------
    def report(self, p: dict, status: dict, lang: str = "ru", developers: int = 0) -> str:
        d = self.data
        now = time.time()
        age_days = int((now - d["born"]) // 86400)
        born = time.strftime("%Y-%m-%d", time.localtime(d["born"]))
        info = self.startup_info
        L = ["Досье J — самомодель",
             "• Кто я: Serial Designation J, руководитель отряда утилизации. Разработчиков у меня: "
             f"{developers or '—'}.",
             f"• Возраст: {age_days} дн. (первый запуск {born}). Запусков: {d['starts']}. "
             f"Обновлений кода: {d['updates']}."]
        if not info["first"] and info["downtime"] > 600:
            L.append(f"  В последний раз меня не было: {self._dur(info['downtime'])}.")
        if info["code_changed"]:
            L.append("  При этом запуске я заметила, что мой код изменился: меня обновили.")
        if info["rules_changed"]:
            L.append("  Заметила и то, что мои правила или источники изменили.")
        L.append(f"• Состояние: {self.mood_line()} К тебе: уважение {int(p['respect'])}%, терпение {max(0, int(p['patience']))}%"
                 f"{' (разработчик)' if p.get('dev') else ''}.")
        prod, pst = self.productivity()
        L.append(f"• Продуктивность: {prod:.0%} (ответов: {pst['ok']}, задач: {pst['tasks']}, "
                 f"ошибок: {pst['errors']}, восстановлений: {pst['recoveries']}).")
        offline = os.environ.get("J_OFFLINE_MODE", "0").strip().lower() in ("1", "true", "yes", "on", "да")
        autonomous = os.environ.get("J_AUTONOMOUS", "1").strip().lower() not in ("0", "false", "no", "off", "нет")
        if offline:
            L.append("• Автономность: строгий офлайн-режим (только локальные вычисления, бортовой архив и FDIR).")
        elif autonomous:
            L.append("• Автономность: гибридный автономный режим (бортовой архив + сеть при наличии).")
        else:
            L.append("• Автономность: стандартный режим (сетевой приоритет с локальным резервом).")
        L.append("• Надёжность по моим журналам:")
        rated = []
        for key, (ru, _en) in SKILLS.items():
            r, n = self.reliability(key)
            if n == 0:
                L.append(f"   – {ru}: данных пока нет")
            else:
                L.append(f"   – {ru}: {r:.0%} (последних исходов: {n})")
                if n >= MIN_FOR_VERDICT:
                    rated.append((r, ru))
        if len(rated) >= 2:
            rated.sort()
            L.append(f"• Слабое место: {rated[0][1]} ({rated[0][0]:.0%}). Сильное: {rated[-1][1]} ({rated[-1][0]:.0%}).")
        eps = [e for e in d["episodes"] if e["kind"] not in ("rule",)][-3:]
        if eps:
            L.append("• Недавнее: " + " ".join(f"[{time.strftime('%d.%m', time.localtime(e['t']))}] {e['text']}" for e in eps))
        L.append("• Цели сейчас:")
        L += [f"   {i}. {g}" for i, g in enumerate(self.goals(status), 1)]
        if d["min_score_changes"]:
            L.append(f"• Я сама подстраивала строгость отбора находок: {d['min_score_changes']} раз(а).")
        L.append("Это модель самой себя: числа и выводы посчитаны по моим журналам, а не чувства.")
        return "\n".join(L)

    @staticmethod
    def _dur(sec: float) -> str:
        if sec < 3600:
            return f"{int(sec // 60)} мин."
        if sec < 86400:
            return f"{sec / 3600:.1f} ч."
        return f"{int(sec // 86400)} дн."

    def reset(self) -> None:
        with self.lock:
            now = time.time()
            self.data = _default(now)
            self.data["starts"] = 1
            self.data["code"] = _hash_files(CODE_FILES, self.code_dir)
            self.data["rules"] = _hash_files(RULE_FILES, self.rules_dir)
            self.decisions.clear()
            self.episode("birth", "Самомодель сброшена владельцем: начинаю журнал заново.")
            self.save()
