#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Реестр игр и клиентов J.

Чтобы добавить новую игру или клиент, достаточно одной записи:

    register(Game(key="newgame", title="Новая игра", prefix="ng", orders=False))

и клиента, который шлёт ядру запросы {"game": "newgame", ...} (см. README → «Как добавить игру»).
Остальное (профили, правила, память, уважение, самомодель) работает сразу.

Поля Game:
  key       — короткое имя, его клиент присылает в поле "game"
  title     — название для сообщений J
  prefix    — префикс профиля игрока: "mc:Ник", "gm:SteamID64"…; None — профиль = чистый ID (так у Discord)
  orders    — разбирать ли в этой игре приказы (False — J только собеседник и советник)
  supported — какие приказы умеет игровая сторона (None — все; пустое множество — никакие)
  events    — свои реплики на события игры; exclusive_events=True — только они, иначе поверх общих
  status    — функция, превращающая данные статуса в фразу J (None — формат Minecraft)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

# --- общие реплики на события (исторически — от Minecraft; другие игры могут переопределять свои)
COMMON_EVENTS = {
    "spawn": ["J на связи. Рабочий день начался. Пиши «j помощь» или просто приказывай.",
              "Смена началась. Я здесь, чтобы руководить. Приказы принимаю в чате: «j руби дерево»."],
    "burning": ["Так. Я горю. Это не по плану. ВОДА. ГДЕ ВОДА?!",
                "Почему я горю?! Кто ответственный?! Ах да… я.",
                "Возгорание на рабочем месте! Срочно ищу воду!"],
    "extinguished": ["Потушена. Если кто-то это видел — вы ничего не видели.",
                     "Пожар ликвидирован. Отчёт об инциденте не требуется. Никому."],
    "no_water": ["Воды рядом нет. Это провал планирования. Не моего."],
    "low_health": ["Здоровье критическое. Это временная неэффективность.",
                   "Мне больно. Занесу в жалобную книгу."],
    "death": ["Отчёт о потерях: я. Причина: обстоятельства.",
              "Я погибла. Формально это увольнение без выходного пособия."],
    "night": ["Стемнело. Мобы — не мой отдел, но я против.",
              "Ночь. Рекомендую кровать. Это приказ."],
    "creeper": ["Крипер. Зелёный, шипит, взрывается. Классический подчинённый N.",
                "Осторожно, крипер! Я не буду это оформлять как несчастный случай."],
    "no_player": ["Не вижу тебя, {user}. Подойди ближе."],
    "queued": ["В очередь. Выполню после текущей задачи."],
    "danger_abort": ["Опасность. Задача прервана. Безопасность выше плана.",
                     "Прерываю работу: здоровье или крипер. Премию не жду."],
    "stuck": ["Застряла. Планировщик в отпуске. Прошу прощения… ни у кого."],
    # дерево
    "chop_progress": ["Квартальный план по древесине: {have} из {n}."],
    "chop_done": ["План по древесине выполнен: {have}. Премию не жду.", "Лесозаготовка завершена: {have}. Эффективность — 100%."],
    "chop_partial": ["Доступные брёвна кончились: {have} из {n}. Остальное — выше моей зоны ответственности."],
    "chop_none": ["Деревьев поблизости нет. Это не мой провал."],
    # камень
    "mine_progress": ["Добыто камня: {have} из {n}."],
    "mine_done": ["Добыча завершена: камня {have}. Производительность в норме."],
    "mine_partial": ["Безопасных блоков больше нет: {have} из {n}. Остальное — риск, а я не N."],
    "mine_none": ["Подходящего камня поблизости нет. Или он слишком опасен."],
    "mine_no_tool": ["Кирки нет. Копать руками — это для N."],
    "pickup_fail": ["Добыча падает, а подобрать не получается. Проверь инвентарь и версию игры."],
    # бой, броня, еда
    "attack_none": ["Враждебных поблизости нет. Скучно."],
    "attack_done": ["Зачистка завершена. Ликвидировано: {kills}."],
    "attack_miss": ["Промах. Даже N с закрытыми глазами машет точнее!",
                    "Мимо. Этот противник уворачивается быстрее, чем твой интернет."],
    "attack_retreat": ["Здоровье критическое. Отступаю. Это не бегство, это тактика."],
    "ammo_low": ["Боезапас на исходе. Пора пополнить снабжение."],
    "armor_done": ["Броня надета: {items} шт. Безопасность превыше KPI."],
    "armor_none": ["Брони в инвентаре нет. Это нарушение техники безопасности."],
    "armor_already": ["Всё уже надето. Лучше уже некуда."],
    "eat_done": ["Подкрепилась. Сытость {food} из 20.", "Обед завершён. Сытость {food} из 20."],
    "eat_full": ["Я не голодна. Еда — это не награда."],
    "eat_none": ["Еды нет. Это провал снабжения."],
    # память мест
    "place_saved": ["Точка «{place}» ({pos}) занесена в реестр координат."],
    "place_reached": ["Прибыла в точку «{place}». Точность по регламенту."],
    "place_not_found": ["Точка «{place}» в базе не найдена."],
    "place_list": ["Сохранённые точки: {places}."],
    "place_empty": ["Список мест пуст. Ни одной точки пока не сохранено."],
    "place_deleted": ["Точка «{place}» удалена из реестра."],
    # крафт
    "craft_done": ["Производство завершено: {item} ({count} шт.). Качество — JCJenson."],
    "craft_no_materials": ["Не хватает материалов для крафта: {item}."],
    "craft_no_table": ["Для крафта «{item}» нужен верстак, но поставить или найти его не удалось."],
    "craft_unknown": ["Рецепт для «{item}» в каталоге не найден."],
}

# --- реплики Garry's Mod: нет воды-спасения, еды и инвентаря Minecraft
GMOD_EVENTS = {
    "spawn": ["J на связи. Рабочий день начался. Пиши в чат: «j за мной», «j атакуй», «j здоровье».",
              "Смена началась. Я на карте. Приказы принимаю в чате: «j ко мне»."],
    "burning": ["Так. Я горю. Это не по плану. Кто ответственный?!", "Возгорание на рабочем месте! Прошу устранить!"],
    "extinguished": ["Потушена. Если кто-то это видел — вы ничего не видели."],
    "low_health": ["Здоровье критическое. Это временная неэффективность.", "Мне больно. Занесу в жалобную книгу."],
    "death": ["Отчёт о потерях: я. Причина: обстоятельства. Возвращаюсь через пару секунд.",
              "Я погибла. Формально это увольнение. Но я вернусь."],
    "attack_none": ["Враждебных поблизости нет. Скучно."],
    "attack_start": ["Обнаружена враждебная цель. Утилизирую в рамках квартального плана."],
    "attack_done": ["Зачистка завершена. Ликвидировано: {kills}."],
    "attack_miss": ["Да ладно, N даже быстрее попадёт при помощи хвоста, чем кто-то с этим оружием, кто это создавал?!",
                    "Промах. Калибровка этого ствола — чистый саботаж JCJenson.",
                    "Цель уклоняется, или у этого оружия разброс как у паники Uzi."],
    "attack_retreat": ["Здоровье критическое. Отступаю. Это не бегство, это тактика."],
    "enemy_spotted": ["Вижу враждебный контакт. Оформляю досрочное увольнение."],
    "ammo_low": ["Боезапас на нуле. Кто отвечал за снабжение отряда?!",
                 "Перезаряжаюсь. Не стойте под линией огня, если вам дорог ваш KPI."],
    "ally_hurt": ["Ты ранен, {user}? Постарайся не портить статистику выживаемости отряда."],
    "kill_streak": ["Серия ликвидаций: {kills}. Вот что значит корпоративная дисциплина."],
    "stuck": ["Застряла в геометрии карты. Тот, кто строил этот уровень, явно не прошёл аудит."],
    "armor_done": ["Экипирована: оружие и броня. Безопасность превыше KPI."],
    "weapon_done": ["Оружие: {name}. Переключилась.", "Взяла: {name}. Готова к работе."],
    "weapon_none": ["Такого оружия у меня нет. Проверь название."],
    "no_player": ["Не вижу тебя, {user}. Подойди ближе."],
    "place_saved": ["Координаты сектора «{place}» ({pos}) записаны в карту."],
    "place_reached": ["Добралась до точки «{place}». Задача выполнена."],
    "place_not_found": ["Точка «{place}» на этой карте не найдена."],
    "place_list": ["Точки на карте: {places}."],
    "place_empty": ["На этой карте сохранённых точек нет."],
    "place_deleted": ["Точка «{place}» удалена с карты."],
}

# --- реплики Left 4 Dead 2
L4D2_EVENTS = {
    "round_start": ["J на связи. Раунд начался. Выжившие, не подведите отряд.",
                    "Смена началась. Я слежу за вашей эффективностью. Это угроза."],
    "tank_spawn": ["Танк! Это не учения. Рассредоточиться!", "Танк на горизонте. Квартальный план по выживанию под угрозой."],
    "witch_spawn": ["Ведьма поблизости. Тише. Даже N это понимает."],
    "player_incapacitated": ["Выживший упал. Поднимите его, это ваш рабочий долг.", "Потеря боеспособности. Оформляйте помощь без бюрократии."],
    "player_death": ["Потери среди выживших. Отчёт будет суровым.", "Выживший погиб. Увольнение без выходного пособия."],
    "mission_lost": ["Миссия провалена. Премии не будет никому.", "Все выжившие потеряны. Провал отряда зафиксирован."],
    "finale_win": ["Эвакуация завершена. Неплохо. Для вас."],
    "finale_start": ["Финал. Держите позицию, не геройствуйте."],
}


# --- реплики физического робота
ROBOT_EVENTS = {
    "boot": ["Системы активированы. Шасси в строю. Я готова руководить — не мешайте.",
             "Загрузка завершена. Все сенсоры в норме, KPI на максимуме."],
    "spawn": ["Системы активированы. Шасси в строю. Я готова руководить — не мешайте."],
    "shutdown": ["Переход в спящий режим. Не трогайте моё шасси без служебной записки."],
    "battery_low": ["Заряд батареи критический: {battery}%. Требую док-станцию, а не советы."],
    "charging": ["На зарядке ({battery}%). Сделайте вид, что работаете самостоятельно."],
    "charged": ["Заряд 100%. Возвращаюсь в строй."],
    "overheat": ["Перегрев узлов ({temp}°C). Снижаю нагрузку — я вам не тостер."],
    "obstacle": ["Препятствие на курсе. Перестраиваю маршрут."],
    "bumper": ["Контакт с препятствием. Сенсор столкновения сработал — отступаю."],
    "stuck": ["Движение заблокировано. Кто оставил этот хлам на пути?"],
    "danger_abort": ["Экстренная остановка: сработал датчик безопасности."],
    "person_seen": ["Обнаружен человек: {user}. Фиксирую присутствие."],
    "person_lost": ["Цель {user} потеряна из поля зрения."],
    "no_player": ["Не вижу тебя, {user}. Подойди в зону сенсоров."],
    "idle_patrol": ["Патрулирую сектор. Пока всё под контролем."],
    "hardware_error": ["Сбой узла: {detail}. Требуется техобслуживание."],
    "place_saved": ["Точка «{place}» ({pos}) записана в карту навигации."],
    "place_reached": ["Прибыла в точку «{place}». Точность по регламенту."],
    "place_not_found": ["Точка «{place}» в карте маршрутов не найдена."],
    "place_list": ["Сохранённые точки маршрута: {places}."],
    "place_empty": ["Карта точек пуста. Ни одной локации пока не сохранено."],
    "place_deleted": ["Точка «{place}» удалена из карты маршрутов."],
}


# --- реплики аэрокосмического и автономного борта (КА / БПЛА)
AEROSPACE_EVENTS = {
    "boot": ["Бортовые системы запущены. Телеметрия в норме, готова к выполнению задач.",
             "Инициализация борта завершена. Все контуры управления активны."],
    "spawn": ["Бортовой модуль на связи. Приказы принимаю в чате, телеметрия под контролем."],
    "shutdown": ["Переход в дежурный спящий режим. Питание ограничено до минимума."],
    "battery_low": ["Внимание: низкий заряд батареи ({battery}%). Требуется стыковка и подзарядка."],
    "charging": ["Идёт зарядка от док-станции ({battery}%)."],
    "charged": ["Батарея заряжена на 100%. Готова к отстыковке и работе."],
    "fuel_low": ["Критический остаток топлива ({fuel}%). Ограничиваю маневрирование."],
    "overheat": ["Перегрев бортовых узлов ({temp}°C). Снижаю нагрузку и включаю термозащиту."],
    "radiation_high": ["Зафиксирована повышенная радиация ({radiation}). Перехожу в защищённый режим."],
    "attitude_lost": ["Потеря ориентирования ({attitude}). Запускаю контур стабилизации."],
    "comms_lost": ["Потеряна внешняя связь. Перехожу на автономное управление по бортовым алгоритмам."],
    "comms_restored": ["Внешняя связь восстановлена. Синхронизирую телеметрию."],
    "obstacle": ["Обнаружено препятствие на траектории. Строю безопасный обход."],
    "bumper": ["Сработал контактный датчик. Остановка и откат от препятствия."],
    "stuck": ["Движение заблокировано. Требуется коррекция траектории."],
    "danger_abort": ["Экстренная остановка: угроза безопасности борта."],
    "docked": ["Стыковка с док-станцией выполнена успешно."],
    "landed": ["Посадка выполнена штатно. Двигатели заглушены."],
    "orbit_stable": ["Орбита стабилизирована. Параметры в расчётных допусках."],
    "maneuver_done": ["Орбитальный манёвр завершён. Траектория выдержана."],
    "scan_done": ["Сканирование сектора завершено. Данные записаны."],
    "stabilized": ["Ориентация стабилизирована. Отклонения в пределах нормы."],
    "safe_mode_enter": ["Борт переведён в безопасный режим. Некритичные нагрузки отключены."],
    "safe_mode_exit": ["Выход из безопасного режима. Штатная работа возобновлена."],
    "hardware_error": ["Аппаратный сбой узла: {detail}. Активирована диагностика FDIR."],
    "place_saved": ["Координаты точки «{place}» ({pos}) занесены в навигационную базу."],
    "place_reached": ["Прибыла в заданную точку «{place}». Точность по регламенту."],
    "place_not_found": ["Навигационная точка «{place}» в базе не найдена."],
    "place_list": ["Навигационные точки маршрута: {places}."],
    "place_empty": ["Навигационная база пуста. Точек маршрута пока нет."],
    "place_deleted": ["Точка «{place}» удалена из навигационной базы."],
}


# подписи действий для сообщений «приступаю» / «не умею»
ACTION_DOING = {
    "follow": "иду за тобой", "come": "иду к тебе", "stop": "стою", "attack": "зачищаю врагов",
    "status": "докладываю состояние", "equip_armor": "экипируюсь", "weapon": "меняю оружие",
    "save_place": "запоминаю точку", "goto_place": "иду к точке",
    "list_places": "смотрю список точек", "forget_place": "удаляю точку",
    "craft": "крафчу предмет",
    "dock": "иду на зарядку к док-станции", "safe_mode": "перехожу в безопасный режим",
    "telemetry": "собираю телеметрию", "scan": "сканирую сектор", "patrol": "патрулирую сектор",
    "takeoff": "выполняю взлёт", "land": "выполняю посадку",
    "stabilize": "стабилизирую ориентацию", "manipulator": "работаю манипулятором",
}
ACTION_CANT = {
    "chop": "рубить дерево", "mine_stone": "добывать камень", "eat": "есть",
    "weapon": "менять оружие", "craft": "крафтить предметы",
    "attack": "атаковать", "equip_armor": "надевать броню",
    "save_place": "запоминать места", "goto_place": "ходить по точкам",
    "list_places": "показывать список мест", "forget_place": "удалять места",
    "dock": "стыковаться с док-станцией", "safe_mode": "переключаться в безопасный режим",
    "telemetry": "собирать телеметрию", "scan": "сканировать сектор", "patrol": "патрулировать сектор",
    "takeoff": "взлетать", "land": "совершать посадку",
    "stabilize": "стабилизировать ориентацию", "manipulator": "управлять манипулятором",
}


def _grade(hp: int, low: int, mid: int) -> str:
    return "Критично." if hp <= low else "Рабочее состояние." if hp <= mid else "Идеально, как и я."


def _safe_num(val, default=None):
    if val is None or isinstance(val, bool):
        return default
    try:
        num = float(val)
        if num != num or num in (float("inf"), float("-inf")):
            return default
        return int(num) if num.is_integer() else round(num, 2)
    except (TypeError, ValueError):
        return default


def status_minecraft(data: dict) -> str:
    hp, food = int(data.get("hp", 0)), int(data.get("food", 0))
    return f"Здоровье {hp} из 20. Сытость {food} из 20. {_grade(hp, 6, 14)}"


def status_gmod(data: dict) -> str:
    hp = int(data.get("hp", 0))
    return f"Здоровье {hp} из 100, броня {int(data.get('armor', 0))}. {_grade(hp, 25, 70)}"


def status_robot(data: dict) -> str:
    data = data if isinstance(data, dict) else {}
    bat_val = _safe_num(data.get("battery"), 100)
    battery = max(0, min(100, int(round(bat_val))))
    parts = [f"Заряд {bat_val}%"]
    temp = _safe_num(data.get("temp"))
    if temp is not None:
        parts.append(f"температура {temp}°C")
    if data.get("mode"):
        parts.append(f"режим: {data['mode']}")
    return ", ".join(parts) + f". {_grade(battery, 20, 60)}"


def status_aerospace(data: dict) -> str:
    """Форматирует телеметрический статус космического аппарата или БПЛА."""
    data = data if isinstance(data, dict) else {}
    bat_val = _safe_num(data.get("battery"), 100)
    battery = max(0, min(100, int(round(bat_val))))
    parts = [f"Заряд {bat_val}%"]
    for key, label in (
        ("fuel", "топливо {v}%"),
        ("altitude", "высота {v} км"),
        ("velocity", "скорость {v} м/с"),
        ("temp", "температура {v}°C"),
    ):
        val = _safe_num(data.get(key))
        if val is not None:
            parts.append(label.format(v=val))
    for key, label in (
        ("attitude", "ориентация: {v}"),
        ("comms", "связь: {v}"),
        ("mode", "режим: {v}"),
    ):
        if data.get(key):
            parts.append(label.format(v=data[key]))
    return ", ".join(parts) + f". {_grade(battery, 20, 60)}"


@dataclass
class Game:
    key: str
    title: str
    prefix: str | None = "x"
    orders: bool = True
    supported: set[str] | None = None
    events: dict[str, list[str]] | None = None
    exclusive_events: bool = False
    status: Callable[[dict], str] | None = None

    def uid(self, ident: str) -> str:
        return f"{self.prefix}:{ident}" if self.prefix else ident

    def event_lines(self, etype: str) -> list[str] | None:
        own = (self.events or {}).get(etype)
        if self.exclusive_events:
            return own
        return own or COMMON_EVENTS.get(etype)

    def status_text(self, data: dict) -> str:
        return (self.status or status_minecraft)(data)


GAMES: dict[str, Game] = {}


def register(game: Game) -> Game:
    GAMES[game.key] = game
    return game


def get_game(key: str) -> Game:
    """Зарегистрированная игра или универсальная запись для неизвестного клиента."""
    return GAMES.get(key) or Game(key=key, title=key, prefix="x")


register(Game("discord", "Discord", prefix=None, orders=False))
register(Game("telegram", "Telegram", prefix="tg", orders=False))
register(Game("whatsapp", "WhatsApp", prefix="wa", orders=False))
register(Game("minecraft", "Minecraft", prefix="mc",
              supported={"follow", "come", "stop", "chop", "mine_stone", "attack", "equip_armor", "status", "eat",
                         "save_place", "goto_place", "list_places", "forget_place", "craft"}))
register(Game("gmod", "Garry's Mod", prefix="gm", events=GMOD_EVENTS, status=status_gmod,
              supported={"follow", "come", "stop", "attack", "status", "equip_armor", "weapon",
                         "save_place", "goto_place", "list_places", "forget_place"}))
register(Game("l4d2", "Left 4 Dead 2", prefix="l4", orders=False, supported=set(), events=L4D2_EVENTS,
              exclusive_events=True))
register(Game("ron", "Ready or Not", prefix="ro", orders=False, supported=set()))
register(Game("robot", "Робот", prefix="rb", events=ROBOT_EVENTS, exclusive_events=True, status=status_robot,
              supported={"follow", "come", "stop", "status", "save_place", "goto_place", "list_places", "forget_place",
                         "dock", "safe_mode", "telemetry", "scan", "patrol", "stabilize", "manipulator"}))
register(Game("aerospace", "Аэрокосмос / КА", prefix="as", events=AEROSPACE_EVENTS, exclusive_events=True,
              status=status_aerospace,
              supported={"follow", "come", "stop", "status", "save_place", "goto_place", "list_places", "forget_place",
                         "dock", "safe_mode", "telemetry", "scan", "patrol", "takeoff", "land", "stabilize",
                         "manipulator"}))
register(Game("drone", "БПЛА / Дрон", prefix="dr", events=AEROSPACE_EVENTS, exclusive_events=True,
              status=status_aerospace,
              supported={"follow", "come", "stop", "status", "save_place", "goto_place", "list_places", "forget_place",
                         "dock", "safe_mode", "telemetry", "scan", "patrol", "takeoff", "land", "stabilize"}))
register(Game("agent", "Межагентный канал (N)", prefix="ag", events=AEROSPACE_EVENTS, exclusive_events=True,
              status=status_aerospace,
              supported={"follow", "come", "stop", "status", "save_place", "goto_place", "list_places", "forget_place",
                         "dock", "safe_mode", "telemetry", "scan", "patrol", "takeoff", "land", "stabilize",
                         "manipulator"}))
