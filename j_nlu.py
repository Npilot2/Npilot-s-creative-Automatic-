#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Понимание приказов для J в Minecraft: намерения, синонимы, контекст, уточнения.

Вход:  текст из игрового чата
Выход: {"reply": "...", "actions": [{"type": "chop", "count": 8}, ...]}  или None (это не приказ)

Что умеет:
  • синонимы и разные формы слов («нарубить», «срубить», «добудь древесины», «мне нужно 5 дерева»)
  • числа цифрами и словами («десять», «стак», «пару», «несколько»)
  • составные приказы: «руби дерево и потом добудь 20 камня»
  • контекст: «ещё», «ещё 5», «теперь камень», «то же самое»
  • неоднозначные фразы: переспрашивает («Что добывать?», «Нарубить дерево?») и помнит ответ
  • обучение прямо в игре: «лесоповал значит рубить дерево»

Типы действий: follow, come, stop, chop, mine_stone, attack, equip_armor, weapon, status, eat,
               save_place, goto_place, list_places, forget_place, craft,
               dock, safe_mode, telemetry, scan, patrol, takeoff, land, stabilize, manipulator
"""
from __future__ import annotations

import random
import re
import time

from j_ai import norm, stems

DEFAULT_COUNT = 8
MAX_COUNT = 64
PENDING_TTL = 90  # секунд, сколько J ждёт ответ на уточнение


def S(words: str) -> set[str]:
    return set(stems(words))


# ---------- словари ----------
CHOP_VERBS = S("руби рубить нарубить срубить нарубай рубай chop fell lumber")
COLLECT_VERBS = S("добудь добыть добывай добыча собери собрать принеси достань накопай накопать копай копать "
                  "mine get gather collect fetch bring dig harvest")
WEAK_VERBS = S("нужно надо нужен нужны нужна хочу need want")  # без объекта приказом не считаются
WOOD = S("дерево деревья деревьев дерева деревце древесина древесины бревно бревна брёвна брёвен бревен "
         "дрова дров лес wood log logs tree trees timber")
STONE = S("камень камня камни камней булыжник булыжника булыжников cobblestone cobble stone stones")

ATTACK_VERBS = S("атакуй атаковать нападай напади бей бить убей убить убивай зачисть зачистить уничтожь "
                 "ликвидируй защити защищай attack kill fight hunt defend slay")
HOSTILE_TARGETS = [
    (S("зомби zombie zombies"), ["zombie", "husk", "drowned", "zombie_villager"], "зомби"),
    (S("скелет скелета скелетов skeleton skeletons"), ["skeleton", "stray"], "скелетов"),
    (S("паук паука пауков spider spiders"), ["spider", "cave_spider"], "пауков"),
    (S("ведьма ведьму ведьм witch"), ["witch"], "ведьм"),
    (S("слайм слайма слаймов slime"), ["slime"], "слаймов"),
]
HOSTILE_GENERIC = S("враждебного враждебных врагов враг врага монстров монстр мобов мобы моба hostile mobs "
                    "monsters enemies enemy")

WEAPON_NAMES = [  # (короткое имя для игры, слова, название)
    ("smg1", S("автомат смг пулемет пулемёт пп smg"), "автомат"),
    ("shotgun", S("дробовик дробовика дробаш шотган shotgun"), "дробовик"),
    ("ar2", S("ar2 импульсная импульсную комбайнов винтовка винтовку"), "импульсная винтовка"),
    ("357", S("револьвер револьвера магнум 357 revolver magnum"), "револьвер"),
    ("crossbow", S("арбалет арбалета арбалетом crossbow"), "арбалет"),
    ("pistol", S("пистолет пистолета пистолетом pistol"), "пистолет"),
    ("crowbar", S("монтировка монтировку монтировкой лом crowbar"), "монтировка"),
    ("stunstick", S("шокер шокером дубинка дубинку stunstick"), "шокер"),
]
WEAPON_GENERIC = S("оружие оружия оружием weapon weapons")
SWITCH_VERBS = S("возьми достань смени сменить переключи переключись используй вооружись бери выбери экипируй "
                 "equip switch use take wield select swap")
AUTO_WORDS = S("автовыбор авто auto автоматически")
ARMOR = S("одень оденься надень надеть экипируй экипируйся броня броню брони доспехи доспех armor armour equip wear dress")
EAT = S("поешь поесть покушай покушать кушай ешь перекуси съешь пообедай поужинай позавтракай eat feed snack")
STATUS = S("здоровье здоровья хп жизни жизней сытость сытости голод статус состояние hp health hunger status")
REPEAT = S("еще опять снова повтори повтор again more same самое")

CRAFT_VERBS = S("скрафти скрафтить крафт крафти сделай сделать смастери смастерить создай создать craft make")
CRAFT_MATERIALS = [
    ("wooden", S("деревянный деревянная деревянное деревянную деревянных деревянные деревянными дерево дерева wood wooden"),
     {"m": "деревянный", "f": "деревянная"}),
    ("stone", S("каменный каменная каменное каменную каменных каменные каменными булыжниковый булыжный булыжник камень камня stone cobble cobblestone"),
     {"m": "каменный", "f": "каменная"}),
    ("iron", S("железный железная железное железную железных железные железными железа железо iron"),
     {"m": "железный", "f": "железная"}),
    ("diamond", S("алмазный алмазная алмазное алмазную алмазных алмазные алмазными алмаз алмаза алмазов diamond"),
     {"m": "алмазный", "f": "алмазная"}),
    ("golden", S("золотой золотая золотое золотую золотых золотые золотыми золото золота gold golden"),
     {"m": "золотой", "f": "золотая"}),
]
# (базовый ключ, слова, русская подпись, поддерживает ли материалы, род подписи)
CRAFT_ITEMS = {
    "planks": (S("доски досок доска доску planks plank"), "доски", False, "m"),
    "stick": (S("палка палки палок палку палочка палочки палочек stick sticks"), "палки", False, "f"),
    "crafting_table": (S("верстак верстака верстаку верстаком table workbench"), "верстак", False, "m"),
    "furnace": (S("печь печи печка печку печки печек furnace"), "печь", False, "f"),
    "chest": (S("сундук сундука сундуку сундуком сундуки сундуков chest chests"), "сундук", False, "m"),
    "torch": (S("факел факела факелы факелов факелу torch torches"), "факел", False, "m"),
    "pickaxe": (S("кирка кирки кирку киркой кирок pickaxe pickaxes"), "кирка", True, "f"),
    "axe": (S("топор топора топору топором топоры топоров axe axes"), "топор", True, "m"),
    "sword": (S("меч меча мечу мечом мечи мечей sword swords"), "меч", True, "m"),
    "shovel": (S("лопата лопаты лопату лопатой лопат shovel shovels"), "лопата", True, "f"),
    "shield": (S("щит щита щиту щитом щиты щитов shield shields"), "щит", False, "m"),
    "bread": (S("хлеб хлеба хлебу хлебом хлебцы bread"), "хлеб", False, "m"),
    "bed": (S("кровать кровати кроватью кроватка bed beds"), "кровать", False, "f"),
}
CRAFT_ALL_STEMS = set().union(*(spec[0] for spec in CRAFT_ITEMS.values()))

PLACE_ALIASES = {
    "домой": "дом", "дома": "дом", "дому": "дом", "дом": "дом",
    "базу": "база", "базе": "база", "базы": "база", "база": "база",
    "место смерти": "смерть", "точку смерти": "смерть", "смерти": "смерть", "смерть": "смерть",
    "спавна": "спавн", "спавну": "спавн", "спавн": "спавн",
}
PLACE_BAD_TARGETS = S("хуй хуя пизд бля нах пох черт чорт жопу баню зад фиг")

R_QUESTION = re.compile(
    r"^(?:(?:а|ну|и|но|подскажи|скажи|объясни|расскажи)\s+)?"
    r"(?:как|что|где|почему|зачем|когда|куда|откуда|можно\s+ли|how|what|why|where|when)\b"
    r"|\b(?:как\s+(?:сделать|скрафтить|создать|смастерить|добыть|найти|получить|построить|работа[ею]т|устроен[аоы]?)"
    r"|how\s+(?:to|do|does|can)|what\s+(?:is|are)|что\s+(?:такое|значит|означает))\b",
    re.I,
)
R_SAFE_MODE = re.compile(
    r"\b(?:"
    r"(?:безопасн|аварийн)\w*\s+режим\w*"
    r"|режим\w*\s+(?:безопасност\w*|аварийн\w*|консерваци\w*)"
    r"|консерваци\w*|законсервиру\w*"
    r"|(?:safe|emergency)\s+mode"
    r")\b",
    re.I,
)
R_DOCK = re.compile(
    r"\b(?:"
    r"(?:на|к|ко|в)\s+(?:зарядк\w*|подзарядк\w*|док(?:\w{0,2})(?:[\s-]+станци\w*)?)"
    r"|баз[ауеы]\s+(?:зарядк\w*|подзарядк\w*)"
    r"|док[\s-]+станци\w*"
    r"|стыков\w*|пристыку\w*|пристыков\w*|стыку\w*"
    r"|причал\w*"
    r"|заряди(?:сь|ться)?|заряжай(?:ся)?"
    r"|(?:return|go|head)\s+(?:to\s+)?dock(?:\s+station)?"
    r"|dock(?:ing)?|charge\s+(?:the\s+)?battery"
    r")\b",
    re.I,
)
R_TELEMETRY = re.compile(
    r"\b(?:"
    r"телеметри\w*|диагностик\w*"
    r"|показани\w*\s+(?:датчик\w*|сенсор\w*|прибор\w*)"
    r"|провер[ьк]\w*\s+систем\w*"
    r"|(?:отч[её]т\w*|сводк\w*)\s+(?:по\s+)?(?:узл\w*|систем\w*|телеметри\w*)"
    r"|telemetry|diagnostics?|sensor\s+report|system\s+check"
    r")\b",
    re.I,
)
R_SCAN = re.compile(
    r"\b(?:"
    r"(?:про)?сканиру\w*|сканировани\w*"
    r"|осмотр[ие]\w*\s+(?:сектор\w*|окрестност\w*|территори\w*|местност\w*)"
    r"|радар\w*|лидар\w*"
    r"|scan(?:\s+sector)?|lidar(?:\s+scan)?|radar(?:\s+scan)?"
    r")\b",
    re.I,
)
R_PATROL = re.compile(
    r"\b(?:"
    r"патрул\w*"
    r"|(?:обход\w*|обходи\w*|охраня\w*|охрана)\s+периметр\w*"
    r"|patrol(?:\s+sector)?|guard\s+(?:the\s+)?perimeter"
    r")\b",
    re.I,
)
R_TAKEOFF = re.compile(
    r"\b(?:"
    r"взл[её]т\w*|подъ[её]м"
    r"|(?:запуск|старт)\s+двигател\w*"
    r"|набер[иу]\w*\s+высот\w*|набор\s+высот\w*"
    r"|take\s*off|lift\s*off|launch"
    r")\b",
    re.I,
)
R_LAND = re.compile(
    r"\b(?:"
    r"посадк\w*|приземл\w*|снижени\w*|снижай\w*"
    r"|садись"
    r"|land(?:ing)?|touchdown"
    r")\b",
    re.I,
)
R_STABILIZE = re.compile(
    r"\b(?:"
    r"стабилиз\w*"
    r"|держи\s+(?:ориентаци\w*|курс|орбит\w*)"
    r"|выровня\w*\s+(?:крен\w*|тангаж\w*|рыскан\w*|ориентаци\w*|курс)"
    r"|коррекци\w*\s+(?:орбит\w*|курс\w*)"
    r"|stabili[zs]\w*|hold\s+(?:attitude|orbit|course)"
    r")\b",
    re.I,
)
R_MANIPULATOR = re.compile(
    r"\b(?:"
    r"манипулятор\w*|захват\w*"
    r"|grab(?:\s+(?:object|target|item))?"
    r"|release\s+gripper|gripper"
    r"|(?:deploy|retract|stow|extend)\s+(?:the\s+)?(?:robotic\s+)?arm"
    r")\b",
    re.I,
)
R_MANIP_RELEASE = re.compile(r"\b(?:отпуст\w*|разжм\w*|брос\w*|слож\w*|убер\w*|release|stow|retract|drop)\b", re.I)
R_MANIP_DEPLOY = re.compile(r"\b(?:разверн\w*|выдвин\w*|активиру\w*\s+манипулятор|deploy|extend)\b", re.I)
R_LIST_PLACES = re.compile(
    r"^(?:какие\s+(?:места|точки)(?:\s+(?:ты\s+)?(?:помнишь|знаешь|есть|сохранены))?"
    r"|список\s+(?:мест|точек)"
    r"|(?:мои|сохраненные)\s+(?:места|точки)"
    r"|(?:list|show|my)\s+(?:places|waypoints)|(?:places\s+list)|waypoints)$",
    re.I,
)
R_FORGET_PLACE = re.compile(
    r"^(?:(?:забудь|удали|сотри|убери)\s+(?:это\s+)?(?:место|точку|локацию|позицию|place|waypoint)\s+(.+)"
    r"|(?:forget|delete|remove)\s+(?:the\s+)?(?:place|waypoint)\s+(.+)"
    r"|(?:забудь|удали)\s+(дом|базу|база|спавн|смерть))$",
    re.I,
)
R_SAVE_PLACE = re.compile(
    r"^(?:(?:запомни|сохрани|добавь|отметь|поставь)\s+(?:это\s+)?(?:место|точку|локацию|позицию|waypoint|place)(?:\s+(?:как\s+)?(.+))?"
    r"|(?:запомни|сохрани)\s+(?:тут|здесь)(?:\s+(.+))?"
    r"|это\s+место(?:\s+(.+))?"
    r"|(?:запомни|сохрани|поставь)\s+(базу|база|дом|спавн)"
    r"|(?:remember|save|mark|set)\s+(?:this\s+)?(?:place|waypoint|spot)(?:\s+(?:as\s+)?(.+))?"
    r"|set\s+home(?:\s+(.+))?)$",
    re.I,
)
R_GOTO_PLACE = re.compile(
    r"^(?:(?:иди|пойди|подойди|беги|ступай|двигай|вернись|возвращайся|перейди)\s+(?:на|к|ко|в|во)\s+(.+)"
    r"|(?:иди|беги|ступай|вернись|возвращайся|пора|пошли)\s+(домой)"
    r"|на\s+(базу)"
    r"|(?:(?:вернись|иди)\s+на\s+|на\s+)?(место\s+смерти)"
    r"|(?:go|walk|run|head|return|come\s+back)\s+(?:to|back\s+to)\s+(.+)"
    r"|(?:go|return|head)\s+(home))$",
    re.I,
)

PHRASES = {
    "stop": [["стой"], ["стоп"], ["хватит"], ["отмена"], ["отменить"], ["останься"], ["жди"], ["прекрати"],
             ["отставить"], ["stop"], ["cancel"], ["stay"], ["wait"], ["abort"], ["enough"]],
    "follow": [["за", "мной"], ["следуй"], ["сопровождай"], ["follow"]],
    "come": [["ко", "мне"], ["иди", "сюда"], ["подойди"], ["сюда"], ["come"]],
}
PHRASE_STEMS = {t: [S(" ".join(ph)) for ph in lst] for t, lst in PHRASES.items()}

GAME_STEMS = S("ножницы бумага бумаги рпс rps paper scissors")  # «камень-ножницы-бумага» — не добыча
AFFIRM = {"да", "ага", "угу", "ок", "окей", "конечно", "давай", "yes", "yeah", "ok", "okay", "sure", "yep"}
NEG = {"нет", "неа", "не", "no", "nope"}

NUM_WORDS = {
    "один": 1, "одну": 1, "одно": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7,
    "восемь": 8, "девять": 9, "десять": 10, "двенадцать": 12, "пятнадцать": 15, "двадцать": 20, "тридцать": 30,
    "сорок": 40, "пятьдесят": 50, "шестьдесят": 60, "дюжину": 12, "пару": 2, "пара": 2, "несколько": 5,
    "немного": 4, "много": 20, "кучу": 30, "стак": 64, "стака": 64, "стек": 64,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "twenty": 20, "thirty": 30, "few": 4, "several": 5, "some": 5, "many": 20, "lot": 20, "stack": 64,
}

R_SPLIT = re.compile(r"\s*(?:,|;|\s+и\s+|\s+а потом\s+|\s+после этого\s+|\s+потом\s+|\s+затем\s+|\s+then\s+|\s+and\s+)\s*")
R_MEANS = re.compile(r"^(.{2,40}?)\s+(?:это значит|значит|означает|means|equals)\s+(.+)$", re.I)

# ---------- реплики J ----------
ACK = {
    "chop": ["Принято. Квартальный план по древесине: {n}. Приступаю.", "Выхожу на лесозаготовку: {n}. Не мешай процессу."],
    "mine_stone": ["Добыча камня: {n}. Приступаю.", "Иду добывать камень, {n} штук. Производительность растёт."],
    "attack": ["Цели обозначены{what}. Ликвидирую.", "Выхожу на зачистку{what}. Держись позади."],
    "equip_armor": ["Надеваю броню. Безопасность превыше KPI."],
    "weapon": ["Меняю оружие: {what}.", "Принято. Оружие: {what}."],
    "eat": ["Обеденный перерыв. Ем.", "Перерыв на обед. Не отвлекай."],
    "follow": ["Иду за тобой. Не потеряйся, {user}.", "Принято, {user}. Следую."],
    "come": ["Иду к тебе. Постарайся не взорвать нас обоих."],
    "stop": ["Стою. Все задачи отменены.", "Отставить. Охраняю позицию."],
    "status": [""],
    "save_place": ["Координаты точки «{place}» зафиксированы.", "Запомнила место «{place}»."],
    "goto_place": ["Выдвигаюсь к точке «{place}».", "Принято. Маршрут: «{place}»."],
    "list_places": [""],
    "forget_place": ["Точка «{place}» списана из памяти.", "Удаляю место «{place}»."],
    "craft": ["Запускаю производство: {what} ({n} шт.).", "Принято. Крафчу: {what}, количество: {n}."],
    "dock": ["Принято. Следую к док-станции на зарядку.", "Курс на стыковочный узел. Иду заряжаться."],
    "safe_mode": ["Активирую безопасный режим. Некритичные подсистемы отключены.", "Переход в режим консервации. Приоритет — сохранность борта."],
    "telemetry": ["Собираю сводку телеметрии и диагностику узлов.", "Запрашиваю показания датчиков и системный отчёт."],
    "scan": ["Запускаю сканирование сектора.", "Лидар и радар активны. Осматриваю сектор."],
    "patrol": ["Выхожу на патрулирование периметра.", "Принято. Начинаю обход сектора."],
    "takeoff": ["Запуск двигателей. Выполняю взлёт.", "Принято. Набираю высоту."],
    "land": ["Начинаю снижение и заход на посадку.", "Принято. Выполняю мягкую посадку."],
    "stabilize": ["Выравниваю ориентацию и стабилизирую курс.", "Принято. Удерживаю заданную ориентацию."],
    "manipulator": ["Манипулятор: выполняю операцию ({what}).", "Принято. Команда манипулятору: {what}."],
}
DEFAULTED = " (Количество не указано — беру {n}.)"
Q_OBJECT = ["Что добывать: дерево или камень?", "Уточни объект: дерево или камень?"]
Q_CONFIRM = {"chop": "Нарубить дерево?", "mine_stone": "Добыть камень?", "attack": "Атаковать «{label}»?"}
NO_REPEAT = ["Повторять нечего: заданий ещё не было."]
CANCELLED = ["Отмена. Ладно."]
TEACH_OK = ["Принято: «{x}» теперь значит «{y}»."]
TEACH_FAIL = ["Не поняла, что значит «{y}». Объясни проще, например «рубить дерево»."]


def parse_count(key: str) -> int | None:
    m = re.search(r"\d+", key)
    if m:
        return max(1, min(MAX_COUNT, int(m.group())))
    for w in key.split():
        if w in NUM_WORDS:
            return max(1, min(MAX_COUNT, NUM_WORDS[w]))
    return None


class ActionNLU:
    def __init__(self, brain) -> None:
        self.brain = brain
        self.pending: dict[str, dict] = {}

    # ---------- публичный вход ----------
    def understand(self, uid: str, user: str, text: str) -> dict | None:
        p = self.brain.profile(uid)
        p.setdefault("syn", {})
        p.setdefault("mc_last", None)
        if not isinstance(p.get("places"), list):
            p["places"] = []
        key = norm(text)
        ks = set(stems(key))
        if not key or ks & GAME_STEMS:
            return None

        m = R_MEANS.match(text.strip())
        if m:
            return self._teach(p, m.group(1), m.group(2))

        pend = self.pending.pop(uid, None)
        if pend and time.time() - pend["ts"] <= PENDING_TTL:
            res = self._resolve_pending(p, user, key, ks, pend)
            if res is not None:
                return res

        if R_QUESTION.search(re.sub(r"[.?!…]+$", "", key).strip()):
            return None

        actions: list[dict] = []
        last = p["mc_last"]
        for clause in [c for c in R_SPLIT.split(key) if c and c.strip()]:
            res = self._clause(p, clause, last)
            if res is None:
                continue
            if res[0] == "ask":
                self.pending[uid] = {"ts": time.time(), "prefix": actions, **res[1]}
                return {"reply": res[2], "actions": []}
            actions.extend(res[1])
            last = actions[-1] if actions else last
        if not actions:
            return None
        return self._finish(p, user, actions)

    # ---------- разбор одного фрагмента ----------
    def _clause(self, p: dict, clause: str, last: dict | None):
        clean_clause = re.sub(r"[.?!…]+$", "", clause).strip()
        ks = set(stems(clean_clause))
        if not ks:
            return None
        tokens = clean_clause.split()
        n = parse_count(clean_clause)

        # 1. синонимы, которым J научили сама
        best = None
        for phrase, act in p["syn"].items():
            ps = set(stems(phrase))
            if ps and ps <= ks and (best is None or len(ps) > best[0]):
                best = (len(ps), act)
        if best:
            act = dict(best[1])
            if n and "count" in act:
                act["count"] = n
            return ("action", [act])

        if R_QUESTION.search(clean_clause):
            return None

        # 2. простые приказы по фразам: стоп важнее всего (и «иди ко мне» важнее goto_place)
        for typ in ("stop", "follow", "come"):
            if any(ph <= ks for ph in PHRASE_STEMS[typ]):
                return ("action", [{"type": typ}])

        # 2а. управление точками (сохранение/удаление/список — раньше робо-приказов)
        place_act = self._place(p, clean_clause, allow_goto=False)
        if place_act:
            return ("action", [place_act])

        # 2б. команды робототехники и аэрокосмоса (раньше goto_place, чтобы «иди на зарядку» / «вернись на док» не стали goto_place)
        robo_act = self._aerospace(clean_clause)
        if robo_act:
            return ("action", [robo_act])

        # 2в. переход к известной точке (waypoints)
        goto_act = self._place(p, clean_clause, allow_manage=False)
        if goto_act:
            return ("action", [goto_act])

        # 2б. крафт (раньше добычи, чтобы «деревянную/каменную кирку» не путать с деревом/камнем)
        craft_act = self._craft(ks, tokens, n, last)
        if craft_act:
            return ("action", [craft_act])

        # 2в. смена оружия: «возьми арбалет», «смени оружие», «оружие авто», «атакуй арбалетом»
        weapon = self._weapon(ks, tokens)
        if weapon:
            if ks & ATTACK_VERBS:
                target, label = self._hostile(ks)
                weapon = [weapon, {"type": "attack", "targets": target, "label": label}]
            else:
                weapon = [weapon]
            return ("action", weapon)

        # 3. слова-группы (порядок = приоритет: «покушай для восстановления здоровья» — это еда, не статус)
        if ks & ARMOR:
            return ("action", [{"type": "equip_armor"}])
        if ks & EAT:
            return ("action", [{"type": "eat"}])
        if ks & STATUS:
            return ("action", [{"type": "status"}])

        # 4. атака
        target, label = self._hostile(ks)
        if ks & ATTACK_VERBS:
            return ("action", [{"type": "attack", "targets": target, "label": label}])
        if (target or ks & HOSTILE_GENERIC) and len(tokens) <= 3:
            act = {"type": "attack", "targets": target, "label": label}
            return ("ask", {"kind": "confirm", "actions": [act]}, Q_CONFIRM["attack"].format(label=label))

        # 5. добыча
        wood, stone = bool(ks & WOOD), bool(ks & STONE)
        chop_v, coll_v, weak_v = ks & CHOP_VERBS, ks & COLLECT_VERBS, ks & WEAK_VERBS
        if not (chop_v or coll_v or weak_v) and (ks & CRAFT_ALL_STEMS):
            wood = stone = False
        kinds = (["chop"] if wood else []) + (["mine_stone"] if stone else [])
        if not kinds and chop_v:
            kinds = ["chop"]
        if kinds and (chop_v or coll_v or weak_v or (n is not None and len(tokens) <= 3)):
            return ("action", [self._gather(k, n) for k in kinds])
        if kinds and len(tokens) <= 3:  # одно слово-объект
            if last and last.get("type") in ("chop", "mine_stone"):  # контекст: «а теперь камень»
                return ("action", [self._gather(k, n) for k in kinds])
            acts = [self._gather(k, n) for k in kinds]
            return ("ask", {"kind": "confirm", "actions": acts}, Q_CONFIRM[kinds[0]].format(label=""))
        if coll_v:
            return ("ask", {"kind": "object", "count": n}, random.choice(Q_OBJECT))

        # 6. «ещё», «ещё 5», «то же самое» — только короткие фразы
        if ks & REPEAT and len(tokens) <= 4:
            if not last or last.get("type") in (None, "stop", "status"):
                return ("ask", {"kind": "none"}, random.choice(NO_REPEAT)) if len(tokens) <= 2 else None
            act = dict(last)
            if n and "count" in act:
                act["count"] = n
            return ("action", [act])
        return None

    @staticmethod
    def _clean_place_name(raw: str, known: list[str] | None = None, *, default: str = "") -> str:
        s = re.sub(r"^[\s,:!«\"'—-]+|[\s.?!»\"'—-]+$", "", norm(raw or ""))
        s = re.sub(r"^(?:под\s+названием|как)\s+", "", s).strip()
        if not s:
            return default
        if s in PLACE_ALIASES:
            return PLACE_ALIASES[s]
        if known:
            def word_stem(w: str) -> str:
                return w[:-1] if len(w) >= 4 and w[-1] in "аяуеиоы" else w

            s_words = [word_stem(w) for w in s.split()]
            for k in known:
                kn = norm(k)
                if kn == s:
                    return k
                k_words = [word_stem(w) for w in kn.split()]
                if len(s_words) == len(k_words) and all(
                    sw == kw or (len(sw) >= 4 and len(kw) >= 4 and sw[:4] == kw[:4])
                    for sw, kw in zip(s_words, k_words)
                ):
                    return k
        return s

    @classmethod
    def _place(cls, p: dict, clause: str, *, allow_manage: bool = True, allow_goto: bool = True) -> dict | None:
        known = p.get("places") or []
        if allow_manage:
            if R_LIST_PLACES.match(clause):
                return {"type": "list_places"}

            m = R_FORGET_PLACE.match(clause)
            if m:
                raw = next((g for g in m.groups() if g), "")
                place = cls._clean_place_name(raw, known)
                if place:
                    return {"type": "forget_place", "place": place}
                return None

            m = R_SAVE_PLACE.match(clause)
            if m:
                raw = next((g for g in m.groups() if g), "")
                place = cls._clean_place_name(raw, default="дом")
                return {"type": "save_place", "place": place}

        if allow_goto:
            m = R_GOTO_PLACE.match(clause)
            if m:
                raw = next((g for g in m.groups() if g), "")
                if norm(raw) == "home":
                    return {"type": "goto_place", "place": "дом"}
                place = cls._clean_place_name(raw, known)
                if not place:
                    return None
                if set(stems(place)) & PLACE_BAD_TARGETS and place not in known:
                    return None
                return {"type": "goto_place", "place": place}
        return None

    @staticmethod
    def _parse_altitude(clean_clause: str) -> int | None:
        m = re.search(r"\d+", clean_clause)
        if m:
            val = int(m.group())
            return min(100000, val) if val > 0 else None
        return parse_count(clean_clause)

    @classmethod
    def _aerospace(cls, clause: str) -> dict | None:
        if R_SAFE_MODE.search(clause):
            return {"type": "safe_mode"}
        if R_DOCK.search(clause):
            return {"type": "dock"}
        if R_TELEMETRY.search(clause):
            return {"type": "telemetry"}
        if R_STABILIZE.search(clause):
            return {"type": "stabilize"}
        if R_MANIPULATOR.search(clause):
            op = "release" if R_MANIP_RELEASE.search(clause) else ("deploy" if R_MANIP_DEPLOY.search(clause) else "grab")
            return {"type": "manipulator", "op": op}
        if R_SCAN.search(clause):
            return {"type": "scan"}
        if R_PATROL.search(clause):
            return {"type": "patrol"}
        if R_TAKEOFF.search(clause):
            alt = cls._parse_altitude(clause)
            return {"type": "takeoff", **({"altitude": alt} if alt else {})}
        if R_LAND.search(clause):
            return {"type": "land"}
        return None

    @staticmethod
    def _craft(ks: set[str], tokens: list[str], n: int | None, last: dict | None) -> dict | None:
        has_verb = bool(ks & CRAFT_VERBS)
        if not has_verb and not (last and last.get("type") == "craft" and len(tokens) <= 3):
            return None
        for item_key, (words, base_label, tiered, gender) in CRAFT_ITEMS.items():
            if not (ks & words):
                continue
            if item_key == "crafting_table" and "верст" not in ks and "table" not in ks and "workb" not in ks:
                continue
            full_key, label = item_key, base_label
            if tiered:
                for mat_key, mat_words, mat_adj in CRAFT_MATERIALS:
                    if ks & mat_words:
                        full_key = f"{mat_key}_{item_key}"
                        label = f"{mat_adj[gender]} {base_label}"
                        break
            return {"type": "craft", "item": full_key, "count": n or 1, "label": label}
        return None

    @staticmethod
    def _weapon(ks: set[str], tokens: list[str]) -> dict | None:
        for name, words, label in WEAPON_NAMES:
            if ks & words and (ks & SWITCH_VERBS or ks & ATTACK_VERBS or len(tokens) <= 2):
                return {"type": "weapon", "name": name, "label": label}
        if ks & WEAPON_GENERIC:
            if ks & AUTO_WORDS:
                return {"type": "weapon", "name": "auto", "label": "автовыбор по ситуации"}
            if ks & SWITCH_VERBS:
                return {"type": "weapon", "name": "next", "label": "следующее по списку"}
        return None

    @staticmethod
    def _hostile(ks: set[str]):
        for group, names, label in HOSTILE_TARGETS:
            if ks & group:
                return names, label
        return None, "враждебных"

    @staticmethod
    def _gather(kind: str, n: int | None) -> dict:
        return {"type": kind, "count": n or DEFAULT_COUNT, "defaulted": n is None}

    # ---------- ответы на уточнения ----------
    def _resolve_pending(self, p: dict, user: str, key: str, ks: set[str], pend: dict) -> dict | None:
        tokens = set(key.split())
        n = parse_count(key)
        prefix = pend.get("prefix", [])
        if pend["kind"] == "confirm":
            if tokens & AFFIRM or (n and len(tokens) <= 2):
                acts = [dict(a) for a in pend["actions"]]
                for a in acts:
                    if n and "count" in a:
                        a["count"], a["defaulted"] = n, False
                return self._finish(p, user, prefix + acts)
            if tokens & NEG:
                return {"reply": random.choice(CANCELLED), "actions": []}  # отказ отменяет весь приказ
        elif pend["kind"] == "object":
            kinds = (["chop"] if ks & WOOD else []) + (["mine_stone"] if ks & STONE else [])
            if kinds:
                return self._finish(p, user, prefix + [self._gather(k, pend.get("count") or n) for k in kinds])
            if tokens & NEG:
                return {"reply": random.choice(CANCELLED), "actions": []}
        return None  # не ответ на вопрос — разбираем как новую фразу

    # ---------- обучение синонимам ----------
    def _teach(self, p: dict, left: str, right: str) -> dict:
        phrase = norm(left).strip()
        res = self._clause({**p, "syn": {}}, norm(right), None)
        if not phrase or not res or res[0] != "action" or len(res[1]) != 1:
            return {"reply": random.choice(TEACH_FAIL).format(y=right.strip()), "actions": []}
        p["syn"][phrase] = {k: v for k, v in res[1][0].items() if k != "defaulted"}
        return {"reply": random.choice(TEACH_OK).format(x=left.strip(), y=right.strip()), "actions": []}

    # ---------- итоговый ответ ----------
    def _finish(self, p: dict, user: str, actions: list[dict]) -> dict:
        places = p.setdefault("places", [])
        if not isinstance(places, list):
            places = p["places"] = []
        lines = []
        for a in actions:
            if a.get("type") == "save_place" and a.get("place"):
                if a["place"] not in places:
                    places.append(a["place"])
            elif a.get("type") == "forget_place" and a.get("place"):
                target_place = norm(a["place"])
                p["places"] = [pl for pl in places if norm(pl) != target_place]
                places = p["places"]
            what = (
                f": {a['label']}" if a.get("type") == "attack" and a.get("targets")
                else a.get("label", "") if a.get("type") in ("weapon", "craft")
                else {"grab": "захват", "release": "сброс захвата", "deploy": "развёртывание"}.get(
                    a.get("op", ""), "захват"
                ) if a.get("type") == "manipulator"
                else ""
            )
            line = random.choice(ACK[a["type"]]).format(
                n=a.get("count", ""), user=user, what=what, place=a.get("place", "")
            )
            if a.get("type") == "takeoff" and a.get("altitude"):
                line += f" Целевая высота: {a['altitude']}."
            if a.get("defaulted"):
                line += DEFAULTED.format(n=a["count"])
            if line:
                lines.append(line)
        repeatable = [a for a in actions if a["type"] not in ("stop", "status", "list_places")]
        if repeatable:
            p["mc_last"] = {k: v for k, v in repeatable[-1].items() if k != "defaulted"}
        clean = [{k: v for k, v in a.items() if k != "defaulted"} for a in actions]
        return {"reply": " ".join(lines), "actions": clean}
