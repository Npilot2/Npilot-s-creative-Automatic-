#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
J как голосовой помощник рядом с игрой (Ready or Not и любой другой).

Это ОТДЕЛЬНАЯ программа: она не трогает процесс игры, не читает память игры, не внедряется и не рисует поверх игры.
Работает так:  зажал кнопку → говоришь → отпустил → J думает (мозг на мосту) → отвечает голосом.

Три режима ввода (J_STT):
  text   — вводишь фразы с клавиатуры (проверка связки без микрофона; режим по умолчанию);
  vosk   — распознавание речи офлайн (pip install vosk sounddevice + русская модель, см. README_VOICE.md).
Голос ответа — встроенный синтезатор Windows (русский голос должен быть установлен в системе).

Настройки (в j.env рядом или переменные среды), все необязательные:
  J_CORE_URL=http://127.0.0.1:8765       адрес ядра J (резервно J_VOICE_BRIDGE)
  J_VOICE_USER=Игрок                     как J будет тебя называть и под каким профилем помнить (ro:<имя>)
  J_STT=text|vosk                        режим ввода
  J_VOSK_MODEL=C:\\путь\\к\\модели        папка модели Vosk (для J_STT=vosk)
  J_PTT_KEY=F9                           кнопка «говорить»: F1–F12, A–Z, 0–9, LCTRL, RCTRL, LALT, RALT, CAPSLOCK,
                                         INSERT, HOME, PAUSE, XBUTTON1, XBUTTON2 (боковые кнопки мыши)
  J_TTS_RATE=0                           скорость речи, от -10 (медленно) до 10 (быстро)
  J_TTS_VOLUME=100                       громкость, 0–100
"""
from __future__ import annotations

import array
import base64
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path

BASE = Path(__file__).resolve().parent
SAMPLE_RATE = 16000
MIN_SPEECH_SECONDS = 0.35   # короче — считаем случайным нажатием
MAX_SPEECH_SECONDS = 30
MAX_SPOKEN_CHARS = 450      # длиннее J не зачитывает вслух (остальное — только на экране)
DEFAULT_WAKE_WORDS = ("эй джей", "слушай джей", "джей", "hey j", "j")


# ---------- настройки ----------
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


def core_url() -> str:
    return os.environ.get("J_CORE_URL") or os.environ.get("J_VOICE_BRIDGE") or "http://127.0.0.1:8765"


VK = {"BACKSPACE": 0x08, "TAB": 0x09, "PAUSE": 0x13, "CAPSLOCK": 0x14, "INSERT": 0x2D, "HOME": 0x24, "END": 0x23,
      "PAGEUP": 0x21, "PAGEDOWN": 0x22, "LCTRL": 0xA2, "RCTRL": 0xA3, "LALT": 0xA4, "RALT": 0xA5, "LSHIFT": 0xA0,
      "RSHIFT": 0xA1, "XBUTTON1": 0x05, "XBUTTON2": 0x06, "MBUTTON": 0x04, "SCROLLLOCK": 0x91}
for _i in range(1, 13):
    VK[f"F{_i}"] = 0x70 + _i - 1
for _i in range(26):
    VK[chr(ord("A") + _i)] = 0x41 + _i
for _i in range(10):
    VK[str(_i)] = 0x30 + _i
    VK[f"NUMPAD{_i}"] = 0x60 + _i


def key_code(name: str) -> int:
    code = VK.get((name or "").strip().upper())
    if code is None:
        raise SystemExit(f"Неизвестная кнопка J_PTT_KEY={name!r}. Доступно: F1–F12, A–Z, 0–9, LCTRL, RCTRL, LALT, RALT, "
                         "CAPSLOCK, INSERT, HOME, PAUSE, XBUTTON1, XBUTTON2.")
    return code


def key_down(code: int) -> bool:
    """Нажата ли кнопка прямо сейчас (опрос состояния, без перехвата ввода игры)."""
    if os.name != "nt":
        return False
    import ctypes
    return bool(ctypes.windll.user32.GetAsyncKeyState(code) & 0x8000)


# ---------- текст для озвучки и wake-word ----------
def clean_for_speech(text: str, limit: int = MAX_SPOKEN_CHARS) -> str:
    """Убирает всё, что не читают вслух: ссылки, источники, значки, Markdown; склеивает строки; режет по границе предложения."""
    t = re.sub(r"https?://\S+", "", text or "")
    t = re.sub(r"\(источник:[^)]*\)", "", t)
    t = re.sub(r"\([^)]*записал[^)]*\)", "", t)
    t = re.sub(r"[⚠•\u2600-\u27BF\U0001F300-\U0001FAFF]", "", t)
    t = re.sub(r"\[[^\]]{0,30}\]", "", t)
    t = t.replace("«", "").replace("»", "")
    t = re.sub(r"(?m)^\s*#+\s*", "", t)
    t = re.sub(r"\*\*|__|~~|`|\*", "", t)
    if re.search(r"[А-Яа-яЁё]", t):
        t = re.sub(r"\bJCJenson\b", "Джей-Си-Дженсон", t, flags=re.IGNORECASE)
        t = re.sub(r"\bKPI\b", "кей-пи-ай", t, flags=re.IGNORECASE)
        t = re.sub(r"\bБПЛА\b", "бе-пе-эл-а", t, flags=re.IGNORECASE)
        t = re.sub(r"\bПИД\b", "пид", t, flags=re.IGNORECASE)
        t = re.sub(r"\bИНС\b", "и-эн-эс", t, flags=re.IGNORECASE)
        t = re.sub(r"\bFDIR\b", "эф-ди-ай-ар", t, flags=re.IGNORECASE)
    parts = [p.strip(" -—:") for p in t.splitlines() if p.strip(" -—:")]
    t = ". ".join(p.rstrip(".") for p in parts)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > limit:
        cut = t[:limit]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        t = cut[: end + 1] if end > limit // 3 else cut.rstrip() + "…"
    return t


def strip_wake_word(
    text: str,
    wake_words: list[str] | set[str] | tuple[str, ...] | None = None,
) -> tuple[bool, str]:
    """Проверяет обращение по wake-word в начале фразы и возвращает (matched, оставшийся_текст)."""
    s = (text or "").strip()
    if not s:
        return False, text
    if wake_words is None:
        candidates = DEFAULT_WAKE_WORDS
    elif isinstance(wake_words, str):
        candidates = [w.strip() for w in wake_words.split(",") if w.strip()]
    else:
        candidates = wake_words
    ordered = sorted({w.strip() for w in candidates if w and w.strip()}, key=len, reverse=True)
    for w in ordered:
        pat = r"\s+".join(re.escape(part) for part in w.split())
        m = re.match(rf"^(?:{pat})(?=$|[\s,:!?.])", s, flags=re.IGNORECASE)
        if m:
            return True, s[m.end():].strip(" ,:-!?.")
    return False, text


# ---------- голос J: встроенный синтезатор Windows ----------
class Speaker:
    def __init__(self, rate: int = 0, volume: int = 100) -> None:
        self.rate = max(-10, min(10, rate))
        self.volume = max(0, min(100, volume))
        self.proc: subprocess.Popen | None = None

    def is_speaking(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def _script(self, lang: str = "ru") -> str:
        culture = "en-US" if lang == "en" else "ru-RU"
        return (
            "Add-Type -AssemblyName System.Speech;"
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
            f"$s.Rate = {self.rate}; $s.Volume = {self.volume};"
            "try { $s.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::NotSet,"
            f" [System.Speech.Synthesis.VoiceAge]::NotSet, 0, (New-Object System.Globalization.CultureInfo('{culture}')))"
            " } catch { };"
            "$s.Speak($env:J_SPEAK_TEXT)"
        )

    def command(self, lang: str = "ru") -> list[str]:
        encoded = base64.b64encode(self._script(lang).encode("utf-16-le")).decode("ascii")
        return ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]

    def speak(self, text: str) -> None:
        self.stop()
        text = clean_for_speech(text)
        if not text:
            return
        lang = "en" if len(re.findall(r"[A-Za-z]", text)) > len(re.findall(r"[А-Яа-яЁё]", text)) else "ru"
        if os.name != "nt":
            print(f"   (голос: {text})")
            return
        env = dict(os.environ, J_SPEAK_TEXT=text)  # текст — через переменную среды: без проблем с кавычками и кириллицей
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.proc = subprocess.Popen(self.command(lang), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         creationflags=flags)
        except OSError as exc:
            print(f"   (не удалось запустить синтезатор речи: {exc})")

    def stop(self) -> None:
        """Прервать речь (например, когда зажали кнопку и хотим сказать новое)."""
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.proc = None

    @staticmethod
    def russian_voice_installed() -> bool | None:
        """True/False — есть ли русский голос; None — проверить не удалось."""
        if os.name != "nt":
            return None
        script = ("Add-Type -AssemblyName System.Speech;"
                  "(New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices() |"
                  " ForEach-Object { $_.VoiceInfo.Culture.Name }")
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                 capture_output=True, text=True, timeout=20).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        return any(line.strip().lower().startswith("ru") for line in out.splitlines())


# ---------- распознавание речи ----------
class VoskSTT:
    def __init__(self, model_path: str) -> None:
        if not model_path or not Path(model_path).is_dir():
            raise SystemExit("Не найдена модель Vosk. Укажи папку модели в J_VOSK_MODEL (см. README_VOICE.md).")
        try:
            from vosk import KaldiRecognizer, Model, SetLogLevel
        except ImportError:
            raise SystemExit("Не установлен vosk: выполни  python -m pip install vosk sounddevice")
        SetLogLevel(-1)
        self._rec_cls = KaldiRecognizer
        self.model = Model(model_path)

    def transcribe(self, pcm: bytes) -> str:
        rec = self._rec_cls(self.model, SAMPLE_RATE)
        rec.AcceptWaveform(pcm)
        return json.loads(rec.FinalResult()).get("text", "").strip()


def pcm_rms(pcm: bytes) -> float:
    """Среднеквадратичная амплитуда (RMS) для 16-битного signed PCM аудио."""
    if not pcm:
        return 0.0
    usable = len(pcm) - (len(pcm) % 2)
    if usable <= 0:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm[:usable] if usable != len(pcm) else pcm)
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


class Recorder:
    def __init__(self) -> None:
        try:
            import sounddevice as sd
        except ImportError:
            raise SystemExit("Не установлен sounddevice: выполни  python -m pip install vosk sounddevice")
        self.sd = sd

    def record_while(self, pressed) -> bytes:
        frames: list[bytes] = []
        block = SAMPLE_RATE // 10
        limit = MAX_SPEECH_SECONDS * 10
        with self.sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=block) as stream:
            while pressed() and len(frames) < limit:
                data, _overflow = stream.read(block)
                frames.append(bytes(data))
        return b"".join(frames)

    def record_vad(
        self,
        rms_threshold: float = 450.0,
        silence_sec: float = 1.0,
        max_sec: float = MAX_SPEECH_SECONDS,
        pre_roll: float = 0.3,
    ) -> bytes:
        """Ждёт появления речи по порогу RMS и записывает до паузы silence_sec или лимита max_sec."""
        block = SAMPLE_RATE // 10
        chunk_sec = block / SAMPLE_RATE
        wait_limit = max(1, int(round(15.0 / chunk_sec)))
        silence_limit = max(1, int(round(silence_sec / chunk_sec)))
        max_chunks = max(1, int(round(max_sec / chunk_sec)))
        pre_len = max(0, int(round(pre_roll / chunk_sec)))
        ring: deque[bytes] = deque(maxlen=pre_len)
        frames: list[bytes] = []

        with self.sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=block) as stream:
            triggered = False
            for _ in range(wait_limit):
                data, _overflow = stream.read(block)
                chunk = bytes(data)
                if pcm_rms(chunk) >= rms_threshold:
                    triggered = True
                    frames.extend(ring)
                    frames.append(chunk)
                    break
                if pre_len:
                    ring.append(chunk)

            if not triggered:
                return b""

            silent = 0
            while len(frames) < max_chunks:
                data, _overflow = stream.read(block)
                chunk = bytes(data)
                frames.append(chunk)
                if pcm_rms(chunk) >= rms_threshold:
                    silent = 0
                else:
                    silent += 1
                    if silent >= silence_limit:
                        break
        return b"".join(frames)


# ---------- связь с мозгом J ----------
_LOCAL_BRIDGE_LOCK = threading.Lock()
_LOCAL_BRIDGE: tuple[str, object] | None = None


def _get_local_bridge():
    """Лениво создаёт встроенный Bridge для автономной работы голоса без запущенного j_core.py."""
    global _LOCAL_BRIDGE
    from j_bridge import Bridge
    from j_paths import data_dir

    cur_dir = str(data_dir())
    with _LOCAL_BRIDGE_LOCK:
        if _LOCAL_BRIDGE is None or _LOCAL_BRIDGE[0] != cur_dir:
            _LOCAL_BRIDGE = (cur_dir, Bridge())
        return _LOCAL_BRIDGE[1]


def ask_bridge(bridge: str, user: str, text: str, timeout: float = 20.0, game: str = "ron") -> str:
    payload = json.dumps({"user": user, "text": text, "game": game}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(bridge.rstrip("/") + "/chat", payload, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")).get("reply", "")
    except Exception as exc:
        standalone = os.environ.get("J_VOICE_STANDALONE", "1").strip().lower() not in ("0", "false", "no", "off", "нет")
        if standalone:
            try:
                return _get_local_bridge().chat(user, text, game=game).get("reply", "")
            except Exception:
                pass
        return f"Мост недоступен ({exc}). Запусти ядро: python j_launcher.py start core"


_STOP_CMDS = {"стоп", "тихо", "замолчи", "хватит говорить", "stop talking", "mute"}
_VOL_UP_CMDS = {"громче", "погромче", "louder", "volume up"}
_VOL_DOWN_CMDS = {"тише", "потише", "quieter", "volume down"}
_RATE_UP_CMDS = {"быстрее", "говори быстрее", "faster"}
_RATE_DOWN_CMDS = {"медленнее", "говори медленнее", "slower"}
_REPEAT_CMDS = {"повтори", "что ты сказала", "повтори ответ", "repeat"}


class VoiceAssistant:
    """Вся логика отдельно от железа, чтобы её можно было проверять без микрофона и игры."""

    def __init__(
        self,
        bridge: str,
        user: str,
        speaker,
        say=print,
        game: str | None = None,
        wake_words: list[str] | None = None,
    ) -> None:
        self.bridge, self.user, self.speaker, self.say = bridge, user, speaker, say
        self.last_reply: str = ""
        self.last_active_ts: float = 0.0
        self.game: str = game or os.environ.get("J_VOICE_GAME", "ron") or "ron"
        if wake_words is not None:
            self.wake_words = list(wake_words)
        else:
            env_wake = os.environ.get("J_WAKE_WORDS", "")
            parsed = [w.strip() for w in env_wake.split(",") if w.strip()]
            self.wake_words = parsed or list(DEFAULT_WAKE_WORDS)

    def handle_text(self, text: str, require_wake: bool = False) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        if require_wake and (time.time() - self.last_active_ts > 15.0):
            matched, stripped = strip_wake_word(text, self.wake_words)
            if not matched:
                return ""
            text = stripped or "привет"

        self.say(f"Ты: {text}")
        cmd = re.sub(r"\s+", " ", text.lower()).strip(" .,!?;:…—-\"'")
        if cmd in _STOP_CMDS:
            self.speaker.stop()
            reply = "Молчу."
            self.say(f"J: {reply}")
            return reply

        reply: str | None = None
        if cmd in _VOL_UP_CMDS:
            self.speaker.volume = min(100, self.speaker.volume + 20)
            reply = f"Громкость: {self.speaker.volume}%."
        elif cmd in _VOL_DOWN_CMDS:
            self.speaker.volume = max(10, self.speaker.volume - 20)
            reply = f"Громкость: {self.speaker.volume}%."
        elif cmd in _RATE_UP_CMDS:
            self.speaker.rate = min(10, self.speaker.rate + 2)
            reply = f"Скорость речи: {self.speaker.rate}."
        elif cmd in _RATE_DOWN_CMDS:
            self.speaker.rate = max(-10, self.speaker.rate - 2)
            reply = f"Скорость речи: {self.speaker.rate}."
        elif cmd in _REPEAT_CMDS:
            reply = self.last_reply if self.last_reply else "Повторять пока нечего."

        if reply is not None:
            self.say(f"J: {reply}")
            self.speaker.speak(reply)
            return reply

        reply = ask_bridge(self.bridge, self.user, text, game=self.game)
        self.last_reply = reply
        self.last_active_ts = time.time()
        self.say(f"J: {reply}")
        self.speaker.speak(reply)
        return reply

    def handle_speech(self, pcm: bytes, stt, require_wake: bool = False) -> str:
        if len(pcm) < int(SAMPLE_RATE * 2 * MIN_SPEECH_SECONDS):
            return ""  # слишком коротко: случайное нажатие
        text = stt.transcribe(pcm)
        if not text:
            if not require_wake:
                self.say("(не расслышала)")
            return ""
        return self.handle_text(text, require_wake=require_wake)


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    load_env()
    bridge = core_url()
    user = os.environ.get("J_VOICE_USER", "Игрок")
    mode = os.environ.get("J_STT", "text").lower()
    speaker = Speaker(int(os.environ.get("J_TTS_RATE", "0") or 0), int(os.environ.get("J_TTS_VOLUME", "100") or 100))
    assistant = VoiceAssistant(bridge, user, speaker)

    voice = Speaker.russian_voice_installed()
    if voice is False:
        print("ВНИМАНИЕ: в Windows нет русского голоса, J будет читать по-русски плохо. "
              "Добавь голос: Параметры → Время и язык → Речь → Добавить голоса.")
    print(f"J — голосовой помощник. Мост: {bridge}. Профиль: ro:{user} ({assistant.game}). Режим ввода: {mode}.")

    if mode == "text":
        print("Пиши фразу и нажимай Enter; J ответит текстом и голосом. Пустая строка или Ctrl+C — выход.")
        while True:
            try:
                line = input("> ")
            except (EOFError, KeyboardInterrupt):
                break
            if not line.strip():
                break
            assistant.handle_text(line)
        speaker.stop()
        return

    if mode != "vosk":
        raise SystemExit(f"Неизвестный режим J_STT={mode!r}: доступно text или vosk.")
    vmode = (os.environ.get("J_VOICE_MODE", "ptt") or "ptt").strip().lower()
    if vmode not in ("ptt", "vad", "wake"):
        raise SystemExit(f"Неизвестный режим J_VOICE_MODE={vmode!r}: доступно ptt, vad или wake.")
    stt = VoskSTT(os.environ.get("J_VOSK_MODEL", ""))
    recorder = Recorder()
    try:
        if vmode == "ptt":
            code = key_code(os.environ.get("J_PTT_KEY", "F9"))
            print(f"Зажми кнопку {os.environ.get('J_PTT_KEY', 'F9').upper()}, говори, отпусти. Ctrl+C — выход.")
            while True:
                if key_down(code):
                    speaker.stop()  # перебили J: замолкает
                    pcm = recorder.record_while(lambda: key_down(code))
                    assistant.handle_speech(pcm, stt)
                time.sleep(0.02)
        else:
            require_wake = vmode == "wake"
            if require_wake:
                print(f"Режим wake: начни с обращения ({', '.join(assistant.wake_words)}). Ctrl+C — выход.")
            else:
                print("Режим vad: говори в микрофон — запись остановится после паузы. Ctrl+C — выход.")
            while True:
                pcm = recorder.record_vad()
                if pcm:
                    assistant.handle_speech(pcm, stt, require_wake=require_wake)
    except KeyboardInterrupt:
        pass
    finally:
        speaker.stop()


if __name__ == "__main__":
    main()
