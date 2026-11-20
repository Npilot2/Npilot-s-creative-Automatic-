#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
J в Telegram. Работает на стандартной библиотеке Python (сторонние пакеты не нужны).

ПО УМОЛЧАНИЮ Telegram-бот — «тонкий клиент»: он передаёт сообщения и медиафайлы ядру J
(python j_core.py или python j_launcher.py start core), поэтому память, самомодель и знания
у J единые с Discord и играми (профиль tg:<telegram_user_id>).
Встроенный режим (без ядра): J_TELEGRAM_EMBEDDED=1.

Настройка в j.env:
    TELEGRAM_TOKEN=123456:ABC-...     # токен от @BotFather
    J_TELEGRAM_ALLOWED=               # необязательно: ID или @username через запятую (пусто = все)
    J_CORE_URL=http://127.0.0.1:8765  # адрес ядра J
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from j_ai import PeerGuard, is_agent_n, is_peer_agent
from j_discord import JDiscordCore, WEB_COMMANDS, classify_attachment, load_env

TG_MAX_LEN = 4000
_MEDIA_MAX_BYTES = 8 * 1024 * 1024


def chunks(text: str, size: int = TG_MAX_LEN):
    while text:
        yield text[:size]
        text = text[size:]


def ask_core(
    url: str,
    uid: str,
    name: str,
    text: str,
    timeout: float = 60.0,
    media: list[dict] | None = None,
) -> str:
    """Отправляет сообщение ядру J и возвращает ответ (профиль tg:<uid>)."""
    body: dict = {"user": name, "id": uid, "text": text, "game": "telegram"}
    if media:
        body["media"] = media
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url.rstrip("/") + "/chat", payload, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")).get("reply", "") or "…"
    except Exception as exc:
        return (
            "Ядро J не отвечает. Запусти его: python j_launcher.py start core "
            f"(адрес {url}; причина: {exc})"
        )


def _sender_name(user: dict) -> str:
    first = (user.get("first_name") or "").strip()
    last = (user.get("last_name") or "").strip()
    full = f"{first} {last}".strip()
    return full or user.get("username") or str(user.get("id") or "TelegramUser")


def _extract_file_refs(msg: dict) -> list[dict]:
    """Собирает ссылки на фото, видео, аудио, голосовые, стикеры и документы из сообщения Telegram."""
    refs: list[dict] = []
    if isinstance(msg.get("photo"), list) and msg["photo"]:
        best = max(msg["photo"], key=lambda p: int(p.get("file_size") or 0))
        refs.append({
            "file_id": best.get("file_id"),
            "filename": "photo.jpg",
            "mime": "image/jpeg",
            "size": int(best.get("file_size") or 0),
            "width": best.get("width"),
            "height": best.get("height"),
        })
    for key, default_name, default_mime in (
        ("video", "video.mp4", "video/mp4"),
        ("animation", "animation.mp4", "video/mp4"),
        ("video_note", "video_note.mp4", "video/mp4"),
        ("audio", "audio.mp3", "audio/mpeg"),
        ("voice", "voice.ogg", "audio/ogg"),
        ("document", "document.bin", "application/octet-stream"),
        ("sticker", "sticker.webp", "image/webp"),
    ):
        obj = msg.get(key)
        if isinstance(obj, dict) and obj.get("file_id"):
            refs.append({
                "file_id": obj.get("file_id"),
                "filename": obj.get("file_name") or default_name,
                "mime": obj.get("mime_type") or default_mime,
                "size": int(obj.get("file_size") or 0),
                "width": obj.get("width"),
                "height": obj.get("height"),
            })
    return refs[:4]


class TelegramBot:
    """Клиент Telegram Bot API с long-polling и поддержкой текста и медиафайлов."""

    def __init__(
        self,
        token: str,
        core_url: str = "http://127.0.0.1:8765",
        embedded: bool = False,
        allowed: set[str] | None = None,
        core: JDiscordCore | None = None,
    ) -> None:
        self.token = token.strip()
        self.api_base = f"https://api.telegram.org/bot{self.token}"
        self.file_base = f"https://api.telegram.org/file/bot{self.token}"
        self.core_url = core_url.rstrip("/")
        self.core = core if core is not None else (JDiscordCore() if embedded else None)
        self.allowed = {x.strip().lstrip("@").lower() for x in (allowed or set()) if x.strip()}
        self.bot_username = ""
        self.offset = 0
        self.peer_guard = PeerGuard()

    def api_call(self, method: str, payload: dict | None = None, timeout: float = 40.0) -> dict:
        data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.api_base}/{method}",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def download_file(self, file_id: str, max_bytes: int = _MEDIA_MAX_BYTES) -> bytes | None:
        info = self.api_call("getFile", {"file_id": file_id}, timeout=20.0)
        if not info.get("ok"):
            return None
        file_path = (info.get("result") or {}).get("file_path")
        if not file_path:
            return None
        file_size = int((info.get("result") or {}).get("file_size") or 0)
        if file_size > max_bytes:
            return None
        with urllib.request.urlopen(f"{self.file_base}/{file_path}", timeout=30.0) as resp:
            raw = resp.read(max_bytes + 1)
        return raw if len(raw) <= max_bytes else None

    def _is_allowed(self, user: dict) -> bool:
        if not self.allowed:
            return True
        uid = str(user.get("id") or "").lower()
        uname = str(user.get("username") or "").lstrip("@").lower()
        return uid in self.allowed or (bool(uname) and uname in self.allowed)

    def _should_reply(self, msg: dict, text: str, has_media: bool) -> bool:
        chat = msg.get("chat") or {}
        if chat.get("type") == "private":
            return bool(text or has_media)
        if self.bot_username and f"@{self.bot_username.lower()}" in text.lower():
            return True
        reply_to = msg.get("reply_to_message") or {}
        reply_from = reply_to.get("from") or {}
        if self.bot_username and str(reply_from.get("username") or "").lower() == self.bot_username.lower():
            return True
        low = text.lower()
        return low.startswith(("/start", "/help", "!note", "!wiki", "!games") + WEB_COMMANDS)

    def extract_media(self, msg: dict) -> list[dict]:
        refs = _extract_file_refs(msg)
        if not refs:
            return []
        out: list[dict] = []
        for ref in refs:
            kind, mime = classify_attachment(ref["filename"], ref["mime"])
            item = {
                "filename": ref["filename"],
                "mime": mime,
                "kind": kind,
                "size": ref["size"],
                "width": ref.get("width"),
                "height": ref.get("height"),
            }
            if ref.get("file_id") and (
                (kind == "text" and ref["size"] <= 256 * 1024)
                or ((kind in ("image", "video", "audio") or mime == "application/pdf") and ref["size"] <= _MEDIA_MAX_BYTES)
            ):
                try:
                    raw = self.download_file(ref["file_id"])
                except Exception:
                    raw = None
                if raw is not None:
                    item["size"] = len(raw)
                    if kind == "text":
                        item["text_preview"] = raw.decode("utf-8", errors="replace")[:1500]
                    else:
                        item["data_b64"] = base64.b64encode(raw).decode("ascii")
            out.append(item)
        return out

    def _ask(self, uid: str, name: str, text: str, media: list[dict] | None, game: str = "telegram") -> str:
        if self.core is not None:
            if game == "agent" and hasattr(self.core.brain, "respond_to_peer") and not media:
                return self.core.brain.respond_to_peer(name or "N", text, uid=f"tg:{uid}")
            return self.core.handle(f"tg:{uid}", name, text, media=media)
        return ask_core(self.core_url, uid, name, text, media=media, game=game)

    def process_update(self, upd: dict) -> str | None:
        msg = upd.get("message") or upd.get("edited_message")
        if not isinstance(msg, dict):
            return None
        user = msg.get("from") or {}
        is_bot = bool(user.get("is_bot"))
        uid = str(user.get("id") or msg.get("chat", {}).get("id") or "0")
        name = _sender_name(user)
        uname = str(user.get("username") or "").lstrip("@")
        chat_id = str((msg.get("chat") or {}).get("id") or "default")
        is_peer = False
        if is_bot:
            is_peer = (
                is_peer_agent(f"tg:{uid}", name)
                or is_peer_agent(uid, uname)
                or is_peer_agent(f"tg:{uid}", uname)
            )
            if not is_peer:
                return None
            if self.bot_username and uname and uname.lower() == self.bot_username.lower():
                return None
            if not (self._is_allowed(user) or is_agent_n(f"tg:{uid}", name) or is_agent_n(uid, uname)):
                return None
        elif not self._is_allowed(user):
            return None
        else:
            self.peer_guard.note_human(f"tg_chat:{chat_id}")

        raw_text = (msg.get("text") or msg.get("caption") or "").strip()
        media = self.extract_media(msg)
        if not self._should_reply(msg, raw_text, bool(media)):
            return None
        if is_peer:
            allowed_peer, _ = self.peer_guard.allow_turn(
                f"tg:{uid}", channel_id=f"tg_chat:{chat_id}", check_cooldown=True
            )
            if not allowed_peer:
                return None

        text = raw_text
        if self.bot_username:
            text = re.sub(rf"@{re.escape(self.bot_username)}\b", "", text, flags=re.I).strip()
        if text.lower().startswith("/start"):
            text = "привет"
        elif text.lower().startswith("/help"):
            text = "помощь"

        reply = self._ask(uid, name, text, media or None, game="agent" if is_peer else "telegram")
        out_chat_id = (msg.get("chat") or {}).get("id")
        msg_id = msg.get("message_id")
        if out_chat_id is not None and reply:
            for part in chunks(reply):
                payload = {"chat_id": out_chat_id, "text": part}
                if msg_id is not None:
                    payload["reply_to_message_id"] = msg_id
                self.api_call("sendMessage", payload, timeout=20.0)
        return reply

    def run(self) -> None:
        me = self.api_call("getMe", timeout=15.0)
        if me.get("ok"):
            self.bot_username = str((me.get("result") or {}).get("username") or "")
        print(f"J в Telegram на связи: @{self.bot_username or '?'}")
        while True:
            try:
                res = self.api_call(
                    "getUpdates",
                    {"offset": self.offset, "timeout": 30, "allowed_updates": ["message"]},
                    timeout=40.0,
                )
                for upd in res.get("result") or []:
                    self.offset = max(self.offset, int(upd.get("update_id", 0)) + 1)
                    try:
                        self.process_update(upd)
                    except Exception as exc:
                        print("[telegram] ошибка обработки:", exc)
            except urllib.error.HTTPError as exc:
                print(f"[telegram] HTTP {exc.code}: {exc.reason}")
                time.sleep(3)
            except Exception as exc:
                print("[telegram] сбой опроса:", exc)
                time.sleep(3)


def main() -> None:
    load_env()
    token = (os.environ.get("TELEGRAM_TOKEN") or "").strip()
    if not token:
        raise SystemExit("Нет TELEGRAM_TOKEN: добавь его в файл j.env (токен от @BotFather).")
    core_url = os.environ.get("J_CORE_URL") or "http://127.0.0.1:8765"
    embedded = os.environ.get("J_TELEGRAM_EMBEDDED", "0").strip().lower() in ("1", "true", "yes", "on", "да")
    allowed = {x.strip() for x in os.environ.get("J_TELEGRAM_ALLOWED", "").split(",") if x.strip()}
    bot = TelegramBot(token=token, core_url=core_url, embedded=embedded, allowed=allowed)
    bot.run()


if __name__ == "__main__":
    main()
