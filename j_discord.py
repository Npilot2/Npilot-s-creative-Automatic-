#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
J в Discord. Нужны j_ai.py, j_nlu.py (для Minecraft), j_web.py рядом и библиотека: pip install -U discord.py

ПО УМОЛЧАНИЮ Discord-бот — «тонкий клиент»: он передаёт слова ядру J (python j_core.py или j_launcher.py start core),
поэтому память, самомодель и знания у J одни на все игры и Discord. Старый режим со своим отдельным мозгом
включается так: J_DISCORD_EMBEDDED=1 (на случай проблем с ядром).

Токен НЕ хранить в коде. Создай рядом файл j.env:
    DISCORD_TOKEN=твой_токен
    J_OWNERS=123456789012345678       # твой Discord ID (владельцы, через запятую): им доступны !study/!review/!approve/!reject
    J_CHANNELS=j-chat,флуд            # необязательно: каналы, где J отвечает на любые сообщения
    J_STUDY_MINUTES=30                # необязательно: как часто J изучает очередную тему (0 = выключить фоновое изучение)
    J_AUTO_TOPICS=1                   # необязательно: 1 = сама записывает темы из вопросов без ответа, 0 = нет
    J_CONTACT=твой_email_или_ник      # необязательно: контакт для User-Agent при обращении к вики
    J_DEVELOPERS=123456789012345678   # Discord ID разработчиков (у них уважение J 100 %); для Minecraft: mc:ТвойНик
    J_RESPECT_DECAY=1                 # необязательно: на сколько пунктов в день «остывает» уважение у остальных
    J_NOTES_OPEN=1                    # необязательно: 0 = общие заметки (!note) добавляют только доверенные

J отвечает: в личке, при упоминании (@J), и в каналах из J_CHANNELS.

Игровой помощник (общая база заметок сервера, j_notes.json):
    !note minecraft | где искать алмазы => твой ответ
    !wiki minecraft | алмазы            (игру можно не указывать)
    !games

Знания из интернета (j_knowledge.json):
    !ask что такое крипер               — ответ из одобренных знаний (всем)
    !topics · !sections                 — очередь тем и разделы, которые J составила сама (всем)
    !study <тема>                       — добавить тему в очередь          (владелец)
    !study now                          — изучить следующую тему сейчас     (владелец)
    !review                             — показать находки на проверку      (владелец)
    !approve 3 5  /  !approve all       — одобрить                          (владелец)
    !reject 4     /  !reject all        — отклонить (больше не вернётся)    (владелец)
    !why                                — объяснить, почему я так ответила (всем)
    !self · !goals · !errors            — самомодель: досье о себе, текущие цели, журнал ошибок и уроков (всем)
    !self reset                         — сбросить самомодель (владелец)

Правила (j_policy.json): предупреждения для спорных тем, пометка «для обучения» для права, отказ на запросы,
помогающие совершить преступление. Подробности — в j_policy.py.
"""
from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import random
import re
import threading
import time
from pathlib import Path

from j_ai import JBrain, PeerGuard, R_BUSY_CLEAR, R_BUSY_SET, is_agent_n, is_peer_agent, jaccard, norm
from j_paths import atomic_write_json, data_path
from j_policy import Policy, Reasoner
from j_self import SelfModel
from j_web import Knowledge

BASE = Path(__file__).resolve().parent
MAX_LEN = 1900  # лимит Discord 2000 символов
DEV_FLOOR = 50  # ниже этого уважение к разработчику не опускается


# ---------- .env без сторонних библиотек ----------
def load_env(path: Path | None = None) -> None:
    path = Path(path) if path else Path(os.environ.get("J_ENV") or BASE / "j.env")
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass


# ---------- заметки по играм (знания пополняют сами игроки) ----------
class Notes:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else data_path("j_notes.json", "J_NOTES")
        try:
            self.data: dict[str, list[dict]] = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def _save(self) -> None:
        atomic_write_json(self.path, self.data, backup=True, indent=1)

    def add(self, game: str, q: str, a: str, author: str) -> None:
        items = self.data.setdefault(norm(game), [])
        for it in items:
            if norm(it["q"]) == norm(q):
                it.update(a=a, by=author)
                break
        else:
            items.append({"q": q, "a": a, "by": author})
        self._save()

    def find(self, query: str, game: str | None = None):
        games = [norm(game)] if game else list(self.data)
        best = None
        for g in games:
            for it in self.data.get(g, []):
                sim = jaccard(it["q"], query)
                if best is None or sim > best[0]:
                    best = (sim, g, it)
        return best if best and best[0] >= 0.3 else None


R_NOTE = re.compile(r"^!note\s+([^|]+)\|\s*(.+?)\s*(?:=>|->|→)\s*(.+)$", re.S | re.I)
R_WIKI = re.compile(r"^!wiki\s+(?:([^|]+)\|\s*)?(.+)$", re.S | re.I)
R_QUESTION = re.compile(
    r"^\s*(?:что такое|кто такой|кто такая|что значит|что означает|как работает|what is|what are|who is|who was|how does)\s+(.+?)\s*\??\s*$",
    re.I)
WEB_COMMANDS = ("!ask", "!topics", "!sections", "!why", "!self", "!goals", "!errors", "!study", "!review", "!approve", "!reject", "!link", "!busy", "!peer")
OWNER_ONLY = ("!study", "!review", "!approve", "!reject")
_DISCORD_PEER_GUARD = PeerGuard()


def is_allowed_bot_message(author_id: str | int = "", username: str = "", display_name: str = "") -> bool:
    """Разрешает сообщения в Discord от дружественного ИИ-агента (например, N) из J_PEER_AGENTS."""
    sid = str(author_id or "").strip()
    return (
        is_peer_agent(sid, username)
        or is_peer_agent(sid, display_name)
        or (bool(sid) and is_peer_agent(f"discord:{sid}", display_name or username))
    )

FACT_INTROS = ["Из моих архивов:", "Справка по запросу:", "Сверилась с досье:"]
NOT_OWNER = ("Это команда владельца. Добавь свой Discord ID в J_OWNERS (файл j.env): "
             "в Discord включи режим разработчика, ПКМ по своему профилю → «Копировать ID пользователя».")


def with_notices(text: str, notices) -> str:
    head = "\n".join(f"⚠ {n}" for n in dict.fromkeys(notices))
    return f"{head}\n\n{text}" if head else text


def format_fact(item: dict, notices=()) -> str:
    body = (f"{random.choice(FACT_INTROS)} {item['text']}\n"
            f"(источник: {item['source']} — {item['url']})")
    return with_notices(body, notices)


def parse_ids(arg: str) -> list[int] | None:
    return [int(x) for x in re.findall(r"\d+", arg)] or None


class JDiscordCore:
    """Вся логика без discord.py — легко тестировать. handle() потокобезопасен (общая блокировка)."""

    def __init__(self, brain: JBrain | None = None, notes: Notes | None = None,
                 knowledge: Knowledge | None = None, owners: set[str] | None = None,
                 policy: Policy | None = None, me: SelfModel | None = None) -> None:
        self.brain = brain or JBrain()
        self.notes = notes or Notes()
        self.knowledge = knowledge or Knowledge()
        self.policy = policy or Policy()
        self.reasoner = Reasoner(self.knowledge, self.policy)
        self.me = me or SelfModel()
        self.me.attach(self.brain)
        self.owners = owners if owners is not None else {
            o.strip() for o in os.environ.get("J_OWNERS", "").split(",") if o.strip()}
        self.auto_topics = os.environ.get("J_AUTO_TOPICS", "1") != "0"
        self.developers = {d.strip() for d in os.environ.get("J_DEVELOPERS", "").split(",") if d.strip()} or set(self.owners)
        self.lock = threading.RLock()
        apply_links = getattr(self.brain, "apply_env_links", None)
        if callable(apply_links):
            apply_links()
        # общую память (классификатор, общие заметки) меняют только доверенные: разработчики и владельцы
        self.me.is_trusted = self.is_trusted
        self.notes_open = os.environ.get("J_NOTES_OPEN", "1") != "0"
        self.purged = self._purge_flagged()

    def _resolve(self, uid: str) -> str:
        fn = getattr(self.brain, "resolve_uid", None)
        return fn(uid) if callable(fn) else uid

    def _is_owner(self, uid: str) -> bool:
        ruid = self._resolve(uid)
        if uid in self.owners or ruid in self.owners:
            return True
        return any(self._resolve(x) == ruid for x in self.owners)

    def _is_developer(self, uid: str) -> bool:
        ruid = self._resolve(uid)
        if uid in self.developers or ruid in self.developers:
            return True
        return any(self._resolve(x) == ruid for x in self.developers)

    def handle(self, uid: str, author: str, text: str = "", media: list[dict] | None = None) -> str:
        with self.lock:
            return self._handle(uid, author, text, media=media)

    def is_trusted(self, uid: str) -> bool:
        return self._is_developer(uid) or self._is_owner(uid)

    # ---------- уважение: 0–100 %, у всех 0, у разработчика 100, шкала живая ----------
    def _sync_respect(self, uid: str, p: dict) -> None:
        uid = self._resolve(uid)
        now = time.time()
        last = p.setdefault("respect_ts", now)
        if self._is_developer(uid):
            p["respect_ts"] = now
            if not p.get("dev"):
                p["dev"], p["respect"] = True, 100
            p["respect"] = max(DEV_FLOOR, min(100, p["respect"]))
            return
        p["dev"] = False
        decay = float(os.environ.get("J_RESPECT_DECAY", "1") or 0)  # без общения уважение остывает на decay пунктов в сутки
        days = int(max(0.0, now - last) // 86400)
        if days >= 1:  # только целыми сутками, чтобы проценты не «плыли» дробями
            p["respect"] = max(0, min(100, p["respect"] - decay * days))
            p["respect_ts"] = last + days * 86400
        else:
            p["respect_ts"] = last
        p["respect"] = max(0, min(100, p["respect"]))

    def _change_respect(self, p: dict, delta: float) -> None:
        floor = DEV_FLOOR if p.get("dev") else 0
        p["respect"] = max(floor, min(100, p["respect"] + delta))

    def _purge_flagged(self) -> int:
        """Убирает из знаний статьи, нарушающие правила (тема самоповреждения, вещества…), в том числе уже одобренные."""
        with self.knowledge.lock:
            bad = [i["id"] for i in self.knowledge.data["items"]
                   if self.policy.assess(i["title"] + " " + i["text"], mode="item")["decision"] != "allow"]
        return self.knowledge.reject(bad) if bad else 0

    def _handle(self, uid: str, author: str, text: str, media: list[dict] | None = None) -> str:
        uid = self._resolve(uid)
        text = (text or "").strip()
        low = text.lower()
        p = self.brain.profile(uid)
        self._sync_respect(uid, p)
        if low.startswith("!games"):
            games = ", ".join(f"{g} ({len(v)})" for g, v in self.notes.data.items())
            return f"Мои досье по играм: {games}" if games else "Досье пусты. Добавь: !note игра | вопрос => ответ"
        m = R_NOTE.match(text)
        if m and not self.notes_open and not self.is_trusted(uid):
            return "Общие заметки сейчас добавляют только доверенные. Скажи владельцу, и он занесёт."
        if m:
            self.notes.add(m.group(1).strip(), m.group(2).strip(), m.group(3).strip(), author)
            self._change_respect(p, 2)  # вклад в знания — шаг к уважению
            return "Заметка занесена в досье. Если соврал — виноват ты."
        m = R_WIKI.match(text)
        if m and low.startswith("!wiki"):
            hit = self.notes.find(m.group(2), m.group(1).strip() if m.group(1) else None)
            if not hit:
                return "В досье такого нет. Добавь сам: !note игра | вопрос => ответ"
            _, game, it = hit
            return f"[{game}] {it['a']}  (записал(а): {it['by']})"
        if low.split(" ", 1)[0] in WEB_COMMANDS:
            return self._web_command(uid, text)

        s = self.brain.session(uid)
        if not p["name"] and not s.state:  # новый собеседник — сначала знакомство
            return self.brain.opening(uid)

        policy_text = "\n".join(
            [text] + [str(m.get("text_preview", "")) for m in (media or []) if isinstance(m, dict) and m.get("text_preview")]
        ).strip()
        if policy_text:
            gate = self.policy.assess(policy_text)  # запросы, помогающие совершить преступление, отсекаются сразу
            if gate["decision"] != "allow":
                self.reasoner.last_trace[uid] = gate["trace"]
                self.me.record(uid, path="policy", trace=gate["trace"])
                self.me.observe("pressure" if gate["decision"] == "pressure" else "refusal", kind=gate["decision"])
                if gate["penalty"]:
                    self._change_respect(p, -gate["penalty"])
                return gate["message"]

        if not s.state and not media and not (R_BUSY_SET.match(text) or R_BUSY_CLEAR.match(text)):
            fallback = self._knowledge_fallback(uid, p, text)
            if fallback:
                spam = getattr(self.brain, "spam", None)
                if spam is not None and spam.check(uid, text):
                    p["patience"] = max(0.0, p["patience"] - 8.0)
                    self.brain.save()
                    return self.brain.say(p, "spam")
                return fallback
        reply = self.brain.respond(uid, text, media=media) if media else self.brain.respond(uid, text)
        s.done = False  # в Discord «пока» не завершает программу
        return reply

    # ---------- знания: ответ из архива и самостоятельный выбор тем ----------
    def _knowledge_fallback(self, uid: str, p: dict, text: str) -> str | None:
        """Если J не распознала фразу и не знает ответа сама — ищет в одобренных знаниях,
        а при вопросе без ответа записывает тему в план изучения."""
        intent, conf = self.brain.model.predict(text)
        taught = self.brain._match_taught(p, text)
        if (intent and conf >= 0.6) or (taught and taught[1] >= 0.5):
            return None
        out = self.reasoner.ask(uid, text)
        self.me.record(uid, path="knowledge", trace=out["trace"])
        if out.get("reply"):
            return with_notices(out["reply"], out["notices"])
        if out.get("hit"):
            self.me.observe("kb_hit")
            return format_fact(out["hit"], self._notices_for(text, out["hit"]))
        m = R_QUESTION.match(text)
        if m and self.auto_topics:
            ok, topic = self.knowledge.queue_topic(self.policy.assess(m.group(1))["expanded"])
            self.me.observe("kb_miss", topic=topic if ok else "")
            if ok:
                return f"Не знаю. Записала тему «{topic}» в план изучения: разберусь и покажу находки владельцу на проверку."
        return None

    def _status(self) -> dict:
        st = self.knowledge.status()
        return {"pending": st["pending"], "approved": st["approved"], "queue": st["queue"]}

    def _notices_for(self, query: str, item: dict) -> list[str]:
        """Предупреждения по вопросу И по самой найденной статье (оружие в тексте — предупреждение, даже если спросили мягко)."""
        return self.policy.assess(query)["notices"] + self.policy.assess(item["title"] + " " + item["text"])["notices"]

    def _cmd_link(self, uid: str, arg: str) -> str:
        link_fn = getattr(self.brain, "link_user", None)
        if not callable(link_fn):
            return "Связывание профилей не поддерживается текущей версией мозга."
        parts = [x.strip() for x in re.split(r"[\s=]+", arg) if x.strip()]
        if not parts or len(parts) > 2:
            return "Формат: !link <id> (например: !link mc:Steve или !link gm:7656119...)"
        if len(parts) == 2:
            if not self._is_owner(uid):
                return NOT_OWNER
            primary, alias = parts[0], parts[1]
        else:
            primary, alias = uid, parts[0]
        users = getattr(self.brain, "users", {})
        if not self._is_owner(uid):
            resolved_alias = self._resolve(alias)
            if (
                self._is_owner(alias)
                or self._is_developer(alias)
                or (isinstance(users, dict) and users.get(resolved_alias, {}).get("dev"))
            ):
                return "Нельзя привязать ID владельца или разработчика без прав владельца."
            if resolved_alias != alias and resolved_alias != self._resolve(primary):
                return "Этот ID уже привязан к другому профилю."
        p_res, a_res = self._resolve(primary), self._resolve(alias)
        if p_res == a_res:
            return f"Профиль {alias} уже связан с {p_res}."
        if not link_fn(primary, alias):
            return "Не удалось связать профили (проверь ID или возможный цикл)."
        canon = self._resolve(primary)
        self._sync_respect(canon, self.brain.profile(canon))
        return f"Профиль {alias} привязан к {canon}."

    def _web_command(self, uid: str, text: str) -> str:
        cmd, _, arg = text.partition(" ")
        cmd, arg = cmd.lower(), arg.strip()
        k = self.knowledge
        if cmd == "!ask":
            if not arg:
                return "Формат: !ask <вопрос>"
            out = self.reasoner.ask(uid, arg)
            self.me.record(uid, path="knowledge" if out.get("decision") == "allow" else "policy", trace=out["trace"])
            if out.get("decision") != "allow":
                self.me.observe("refusal", kind=out.get("decision"))
                return out["reply"]
            if out.get("reply"):
                return with_notices(out["reply"], out["notices"])
            if out.get("hit"):
                self.me.observe("kb_hit")
                return format_fact(out["hit"], self._notices_for(arg, out["hit"]))
            self.me.observe("kb_miss")
            return "В моих архивах этого нет. Добавь тему: !study <тема> (владелец) или спроси иначе."
        if cmd == "!why":
            return self.me.explain(uid)
        if cmd == "!goals":
            return "Мои цели сейчас:\n" + "\n".join(f"{n}. {g}" for n, g in enumerate(self.me.goals(self._status()), 1))
        if cmd == "!errors":
            return self.me.errors()
        if cmd == "!self" and arg.lower() != "reset":
            return self.me.report(self.brain.profile(uid), self._status(), self.brain.profile(uid)["lang"] or "ru",
                                  len(self.developers))
        if cmd == "!sections":
            sec = k.sections()
            if not sec:
                return "Разделов пока нет: я ещё ничего не изучала."
            return "Мои разделы (составила сама):\n" + "\n".join(
                f"• {name}: одобрено {v['approved']}, на проверке {v['pending']}" for name, v in sec.items())
        if cmd == "!topics":
            st = k.status()
            queue = ", ".join(st["queue"][:10]) or "пусто"
            return (f"Очередь тем: {queue}\nИзучено тем: {st['studied']} · на проверке: {st['pending']} · "
                    f"одобрено: {st['approved']}")
        if cmd == "!link":
            return self._cmd_link(uid, arg)
        if cmd == "!busy":
            low_arg = arg.strip().lower()
            if not low_arg or low_arg in ("status", "статус", "info"):
                if self.brain.is_busy():
                    return f"Занята задачей: «{self.brain.current_task()}»."
                return "Свободна, дежурный режим."
            if self.owners and not self._is_owner(uid):
                return NOT_OWNER
            if low_arg in ("off", "clear", "stop", "none", "нет", "свободна"):
                self.brain.clear_busy()
                return "Задача закрыта. Свободна и перехожу в дежурный режим."
            self.brain.set_busy(arg.strip(), source="discord")
            return f"Принято: занята задачей «{self.brain.current_task()}». Если напишет N — отвечу ему по ходу дела."
        if cmd == "!peer":
            low_arg = arg.strip().lower()
            if not low_arg or low_arg in ("status", "статус", "info"):
                busy_txt = f"занята («{self.brain.current_task()}»)" if self.brain.is_busy() else "свободна"
                peers = os.environ.get("J_PEER_AGENTS", "N,Serial Designation N,SD-N,н,дрон н")
                n_url = os.environ.get("J_N_URL", "").strip() or "не задан"
                return f"Межагентный канал N ↔ J: статус J — {busy_txt}; разрешённые агенты: {peers}; J_N_URL: {n_url}."
            if self.owners and not self._is_owner(uid):
                return NOT_OWNER
            peer_name = "N"
            msg_text = arg.strip()
            if ":" in msg_text:
                head, tail = msg_text.split(":", 1)
                if head.strip() and is_peer_agent("", head.strip()):
                    peer_name = head.strip()
                    msg_text = tail.strip()
            return self.brain.respond_to_peer(peer_name, msg_text, uid=f"ag:{norm(peer_name) or 'n'}")
        if not self._is_owner(uid):
            return NOT_OWNER
        if cmd == "!self":  # сюда доходит только «!self reset»
            self.me.reset()
            return "Самомодель сброшена. Начинаю журнал о себе заново."
        if cmd == "!study":
            if arg.lower() == "now":
                stats = k.study_next()
                if not stats:
                    return "Очередь тем пуста или полка проверки переполнена. Разбери находки: !review"
                self.me.observe("studied", topic=stats["topic"], added=stats["added"])
                return (f"Изучила «{stats['topic']}»: найдено {stats['found']}, на проверку {stats['added']}, "
                        f"отсеяла {stats['skipped']}. Смотри: !review")
            gate = self.policy.assess(arg)
            if gate["decision"] != "allow":
                return gate["message"]
            ok, info = k.queue_topic(gate["expanded"])
            reasons = {"invalid": "Тема не подходит: пусто, слишком длинно или есть личные данные/ссылки.",
                       "known": "Эту тему я уже знаю или она в очереди.", "full": "Очередь тем полна."}
            return f"Тема «{info}» принята в план изучения." if ok else reasons.get(info, "Не получилось.")
        if cmd == "!review":
            removed = self._purge_flagged()
            items = k.pending(5)
            if removed:
                s_removed = f"Убрала {removed} находок(и), нарушающих правила (самоповреждение, вещества и т.п.).\n\n"
            else:
                s_removed = ""
            if not items:
                return s_removed + "На проверке пусто. Либо нечего показывать, либо ещё не изучала."
            return s_removed + "Находки на проверку:\n\n" + "\n\n".join(
                f"#{i['id']} [{i['section']}] {i['title']} — оценка {i['score']}\n{i['text'][:260]}"
                f"{'…' if len(i['text']) > 260 else ''}\n{i['url']}"
                + "".join(f"\n⚠ {n}" for n in self.policy.assess(i['title'] + ' ' + i['text'])['notices'])
                for i in items
            ) + "\n\n!approve <номера|all> — одобрить, !reject <номера|all> — отклонить"
        if cmd in ("!approve", "!reject"):
            ids = None if arg.lower() == "all" else parse_ids(arg)
            if ids is None and arg.lower() != "all":
                return f"Формат: {cmd} 3 5  или  {cmd} all"
            n = k.approve(ids) if cmd == "!approve" else k.reject(ids)
            self.me.observe("approved" if cmd == "!approve" else "rejected", n=n)
            k.min_score = self.me.tune_min_score(k.min_score)  # J сама подстраивает строгость своего отбора
            return f"{'Одобрено' if cmd == '!approve' else 'Отклонено'}: {n}."
        return "Не знаю такой команды."


_MEDIA_MAX_BYTES = 8 * 1024 * 1024
_TEXT_EXTS = {".txt", ".md", ".py", ".js", ".ts", ".json", ".csv", ".log", ".ini", ".cfg", ".yaml", ".yml", ".xml", ".html", ".css"}
_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
_AUDIO_EXTS = {".mp3", ".ogg", ".wav", ".flac", ".m4a", ".opus", ".aac"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_DOC_EXTS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".rtf"}


def classify_attachment(filename: str, content_type: str | None = None) -> tuple[str, str]:
    ext = Path(filename or "").suffix.lower()
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if not mime:
        mime = mimetypes.guess_type(filename or "")[0] or "application/octet-stream"
    if mime.startswith("image/") or ext in _IMAGE_EXTS:
        return "image", mime
    if mime.startswith("video/") or ext in _VIDEO_EXTS:
        return "video", mime
    if mime.startswith("audio/") or ext in _AUDIO_EXTS:
        return "audio", mime
    if mime.startswith("text/") or mime in ("application/json", "application/xml") or ext in _TEXT_EXTS:
        return "text", mime
    if mime == "application/pdf" or ext in _DOC_EXTS:
        return "document", mime
    return "other", mime


async def extract_attachments(attachments, limit: int = 4) -> list[dict]:
    out: list[dict] = []
    for att in list(attachments or [])[:limit]:
        fname = getattr(att, "filename", None) or "file"
        ctype = getattr(att, "content_type", None)
        size = int(getattr(att, "size", 0) or 0)
        kind, mime = classify_attachment(fname, ctype)
        item = {
            "filename": fname,
            "mime": mime,
            "kind": kind,
            "size": size,
            "width": getattr(att, "width", None),
            "height": getattr(att, "height", None),
        }
        try:
            if kind == "text" and size <= 256 * 1024:
                raw = await att.read()
                item["text_preview"] = raw.decode("utf-8", errors="replace")[:1500]
            elif (kind in ("image", "video", "audio") or mime == "application/pdf") and size <= _MEDIA_MAX_BYTES:
                raw = await att.read()
                item["data_b64"] = base64.b64encode(raw).decode("ascii")
        except Exception:
            pass
        out.append(item)
    return out


def ask_core(url: str, uid: str, name: str, text: str, timeout: float = 60.0,
             media: list[dict] | None = None, game: str = "discord") -> str:
    """Отправляет сообщение ядру J и возвращает ответ. Профиль в ядре = чистый Discord ID (game=discord) или agent."""
    import urllib.request
    body = {"user": name, "id": uid, "text": text, "game": game}
    if media:
        body["media"] = media
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url.rstrip("/") + "/chat", payload, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")).get("reply", "") or "…"
    except Exception as exc:
        return ("Ядро J не отвечает. Запусти его: python j_launcher.py start core "
                f"(адрес {url}; причина: {exc})")


def chunks(text: str, size: int = MAX_LEN):
    while text:
        yield text[:size]
        text = text[size:]


def main() -> None:
    import discord  # импорт здесь, чтобы ядро можно было тестировать без библиотеки

    load_env()
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Нет DISCORD_TOKEN: создай файл j.env рядом со скриптом.")
    channels = {c.strip().lower() for c in os.environ.get("J_CHANNELS", "").split(",") if c.strip()}
    embedded = os.environ.get("J_DISCORD_EMBEDDED", "0").strip().lower() in ("1", "true", "yes", "on", "да")
    core_url = os.environ.get("J_CORE_URL") or os.environ.get("J_VOICE_BRIDGE") or "http://127.0.0.1:8765"
    # фоновое изучение — дело ядра; в старом встроенном режиме им занимается сам бот
    study_minutes = float(os.environ.get("J_STUDY_MINUTES", "30") or 0) if embedded else 0.0

    intents = discord.Intents.default()
    intents.message_content = True  # включить в Developer Portal → Bot → Message Content Intent
    core = JDiscordCore() if embedded else None

    async def study_loop():
        """Фоновое самообучение: раз в J_STUDY_MINUTES минут J изучает очередную тему из очереди."""
        await client.wait_until_ready()
        while not client.is_closed():
            await asyncio.sleep(study_minutes * 60)
            try:
                stats = await asyncio.to_thread(core.knowledge.study_next)
                if stats:
                    core.me.observe("studied", topic=stats["topic"], added=stats["added"])
                    print(f"[изучение] «{stats['topic']}»: найдено {stats['found']}, "
                          f"на проверку {stats['added']}, отсеяно {stats['skipped']}")
            except Exception:
                import traceback
                traceback.print_exc()

    class JClient(discord.Client):
        async def setup_hook(self):
            if study_minutes > 0:
                self.loop.create_task(study_loop())

    client = JClient(intents=intents)

    @client.event
    async def on_ready():
        print(f"J на связи как {client.user}")
        print("Это окно — только журнал, писать сюда нельзя. Пиши боту в Discord: "
              "в личку, через @J в канале или в канале из J_CHANNELS.")
        if core is None:
            print(f"Режим: клиент ядра J ({core_url}).")
            try:
                import urllib.request
                with urllib.request.urlopen(core_url.rstrip("/") + "/health", timeout=3) as resp:
                    print("Ядро отвечает:", json.loads(resp.read().decode("utf-8")))
            except Exception as exc:
                print(f"ВНИМАНИЕ: ядро J сейчас не отвечает ({exc}). Запусти: python j_launcher.py start core")
        else:
            print("Режим: встроенный мозг (J_DISCORD_EMBEDDED=1), память отдельная от ядра.")
            info = core.me.startup_info
            print("Самомодель: " + ("первый запуск." if info["first"] else
                  f"запуск №{core.me.data['starts']}" + (", код обновлён" if info["code_changed"] else "") +
                  (", правила изменены" if info["rules_changed"] else "") + "."))
            if core.policy.error:
                print("ВНИМАНИЕ (правила):", core.policy.error)
            if not core.owners:
                print("ВНИМАНИЕ: J_OWNERS не задан — команды !study/!review/!approve/!reject никому не доступны.")
            if study_minutes > 0:
                print(f"Фоновое изучение: раз в {study_minutes:g} мин. (J_STUDY_MINUTES=0 — выключить)")
        try:
            url = discord.utils.oauth_url(
                client.user.id,
                permissions=discord.Permissions(view_channel=True, send_messages=True, read_message_history=True),
                scopes=["bot"],
            )
            print("Ссылка-приглашение на сервер:", url)
        except Exception:
            pass

    @client.event
    async def on_message(msg: "discord.Message"):
        if client.user and msg.author.id == client.user.id:
            return
        is_bot = bool(msg.author.bot)
        ch_id = str(getattr(msg.channel, "id", "") or ("dm" if msg.guild is None else "default"))
        if is_bot:
            if not is_allowed_bot_message(
                msg.author.id,
                getattr(msg.author, "name", "") or "",
                getattr(msg.author, "display_name", "") or "",
            ):
                return
            allowed_peer, _ = _DISCORD_PEER_GUARD.allow_turn(
                f"discord:{msg.author.id}", channel_id=ch_id, check_cooldown=True
            )
            if not allowed_peer:
                return
        else:
            _DISCORD_PEER_GUARD.note_human(ch_id)
        is_dm = msg.guild is None
        mentioned = client.user in msg.mentions
        in_channel = getattr(msg.channel, "name", "").lower() in channels
        is_cmd = (not is_bot) and msg.content.startswith(("!note", "!wiki", "!games") + WEB_COMMANDS)
        if not (is_dm or mentioned or in_channel or is_cmd):
            return
        text = re.sub(rf"<@!?{client.user.id}>", "", msg.content).strip()
        if not text and not msg.attachments:
            return
        async with msg.channel.typing():
            try:
                media = await extract_attachments(msg.attachments) if msg.attachments else None
                # в отдельном потоке: сетевые команды (!study now) не должны подвешивать бота
                if core is not None:
                    if is_bot:
                        reply = await asyncio.to_thread(
                            core.brain.respond_to_peer,
                            msg.author.display_name or getattr(msg.author, "name", "N"),
                            text,
                            f"discord:{msg.author.id}",
                        )
                    else:
                        reply = await asyncio.to_thread(core.handle, str(msg.author.id), msg.author.display_name, text, media)
                else:
                    reply = await asyncio.to_thread(
                        ask_core, core_url, str(msg.author.id), msg.author.display_name, text, 60.0, media,
                        "agent" if is_bot else "discord",
                    )
            except Exception:  # ошибка не должна превращаться в молчание
                import traceback
                traceback.print_exc()
                reply = "Внутренний сбой. Подробности в консоли — покажи их разработчику."
        if not reply:
            return
        for part in chunks(reply):
            await msg.reply(part, mention_author=False)

    client.run(token)


if __name__ == "__main__":
    main()
