#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
J учится по интернету: поиск, оценка, самостоятельная группировка по разделам, проверка владельцем.

Как это работает
  1. ТЕМЫ: владелец задаёт их командой, а J сама добавляет темы из вопросов, на которые не смогла ответить.
  2. ПОИСК: только по сайтам из белого списка (j_sources.json) через официальный API MediaWiki
     (Википедия, Minecraft Wiki…). Только https, таймауты, лимит размера и числа запросов.
  3. ОЦЕНКА: J сама решает, что стоит сохранить: релевантность теме, качество текста, доверие к источнику;
     дубликаты, страницы-неоднозначности и подозрительные тексты отбрасываются.
  4. РАЗДЕЛЫ (обучение без учителя): найденное группируется по сходству TF-IDF без заранее заданных тем;
     название раздела J придумывает по самым характерным словам.
  5. ПРОВЕРКА: находки попадают в «непроверенное». В основную память — только после !approve.
  6. ИСПОЛЬЗОВАНИЕ: одобренное J использует в ответах, всегда указывая источник.

Безопасность: всё скачанное — только данные, а не инструкции; J ничего не выполняет из страниц;
хранится короткая выжимка (до 700 символов) со ссылкой на источник, а не копия страницы.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

from j_ai import norm, stems

BASE = Path(__file__).resolve().parent
from j_paths import data_path

MAX_BYTES = 1_000_000      # больше этого ответ не читаем
MAX_TEXT = 700             # длина хранимой выжимки
MIN_SCORE = 0.6            # ниже — J сама отбрасывает находку
MAX_PENDING = 60           # сколько находок может ждать проверки
MAX_QUEUE = 30             # очередь тем
PER_TOPIC = 3              # лучших находок на тему
DAILY_CAP = 300            # запросов в сутки
CLUSTER_SIM = 0.08         # порог сходства для объединения в раздел (близкие тексты ~0.12, чужие ~0.03)

DEFAULT_SOURCES = [
    {"name": "Википедия (ru)", "api": "https://ru.wikipedia.org/w/api.php", "trust": 0.9, "license": "CC BY-SA 4.0"},
    {"name": "Wikipedia (en)", "api": "https://en.wikipedia.org/w/api.php", "trust": 0.9, "license": "CC BY-SA 4.0"},
    {"name": "Minecraft Wiki", "api": "https://minecraft.wiki/api.php", "trust": 0.8, "license": "см. лицензию на сайте"},
]

STOP_Q = set(stems("что такое кто такой такая это значит означает как работает где what is are who the a an of in how does was"))
STOP_WORDS = set(stems("который которые которая которой может могут если также этот этого этой после между более когда чтобы такой свои своей потому даже очень его ее их они она оно был была были быть для при про над под без чем что как так там тут уже еще или либо from with that this which have been also their there than into over such"))
INJECTION = re.compile(
    r"(ignore\s+(all\s+|any\s+)?(previous|above|prior)|disregard\s.{0,30}instruction|system\s+prompt|you\s+are\s+now"
    r"|игнорируй\s.{0,30}(инструкц|предыдущ)|забудь\s.{0,20}инструкц|ты\s+теперь\s)", re.I)
DISAMBIG = re.compile(r"(может означать|may refer to|may also refer to|значени[ея]:|disambiguation)", re.I)
PRIVATE = re.compile(r"(@|https?://|\d{6,})")
_WORDS = re.compile(r"[\w']+", re.UNICODE)


def is_offline_mode() -> bool:
    """Проверяет принудительный офлайн-режим (J_OFFLINE_MODE=1/true/yes/on/да)."""
    return os.environ.get("J_OFFLINE_MODE", "0").strip().lower() in ("1", "true", "yes", "on", "да")


def _truthy_env(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on", "да")


# ---------- встроенная офлайн-энциклопедия аэрокосмоса и робототехники ----------
BUILTIN_AEROSPACE_KB: list[dict] = [
    {
        "id": "builtin-1",
        "status": "approved",
        "source": "Бортовой архив J (офлайн)",
        "url": "offline://aerospace-robotics/tsiolkovsky-rocket-equation",
        "score": 1.0,
        "title": "Формула Циолковского (Tsiolkovsky rocket equation)",
        "topic": "формула циолковского tsiolkovsky rocket equation",
        "section": "Баллистика и орбитальная механика",
        "text": (
            "Основное уравнение ракетной динамики (Tsiolkovsky rocket equation): характеристическая скорость "
            "Δv = I_sp · g0 · ln(m0 / mf) = v_e · ln(m0 / mf), где I_sp — удельный импульс (specific impulse, с), "
            "g0 ≈ 9.80665 м/с², v_e — эффективная скорость истечения (exhaust velocity, м/с), m0 — начальная масса "
            "(wet mass), mf — конечная сухая масса (dry mass). Описывает идеальный импульс без гравитационных и "
            "аэродинамических потерь; для вывода на низкую околоземную орбиту (LEO) требуется Δv ≈ 9.3–10 км/с."
        ),
    },
    {
        "id": "builtin-2",
        "status": "approved",
        "source": "Бортовой архив J (офлайн)",
        "url": "offline://aerospace-robotics/orbital-mechanics-hohmann-transfer",
        "score": 1.0,
        "title": "Орбитальная механика и гомановский переход (Orbital mechanics & Hohmann transfer)",
        "topic": "орбитальная механика гомановский переход orbital mechanics hohmann transfer",
        "section": "Баллистика и орбитальная механика",
        "text": (
            "Орбитальная механика (orbital mechanics) описывает кеплерово движение аппаратов в гравитационном поле: "
            "круговая скорость v = sqrt(μ / r), скорость убегания v_esc = sqrt(2μ / r), период обращения "
            "T = 2π·sqrt(a³ / μ), где μ = GM (для Земли μ ≈ 398600.4 км³/с²). Первая космическая скорость у Земли "
            "≈ 7.91 км/с, вторая ≈ 11.19 км/с. Гомановский переход (Hohmann transfer) — двухимпульсный эллиптический "
            "манёвр минимального расхода Δv между копланарными круговыми орбитами r1 и r2 через перигей и апогей."
        ),
    },
    {
        "id": "builtin-3",
        "status": "approved",
        "source": "Бортовой архив J (офлайн)",
        "url": "offline://aerospace-robotics/pid-controller",
        "score": 1.0,
        "title": "ПИД-регулятор (PID controller)",
        "topic": "пид регулятор pid controller",
        "section": "Системы управления и навигации",
        "text": (
            "ПИД-регулятор (PID controller — Proportional-Integral-Derivative) формирует управляющий сигнал "
            "u(t) = Kp·e(t) + Ki·∫e(τ)dτ + Kd·de/dt по ошибке e(t) = r(t) − y(t). Пропорциональное P-звено даёт "
            "быстрый отклик, интегральное I-звено устраняет статическую ошибку (при насыщении актуаторов нужна "
            "защита anti-windup), дифференциальное D-звено демпфирует колебания и перерегулирование (overshoot). "
            "В роботах и БПЛА используют каскадные контуры (позиция → скорость → угловая скорость) с фильтрацией D-канала."
        ),
    },
    {
        "id": "builtin-4",
        "status": "approved",
        "source": "Бортовой архив J (офлайн)",
        "url": "offline://aerospace-robotics/kalman-filter-sensor-fusion",
        "score": 1.0,
        "title": "Фильтр Калмана и комплексирование датчиков (Kalman filter & sensor fusion)",
        "topic": "фильтр калмана комплексирование датчиков kalman filter sensor fusion",
        "section": "Системы управления и навигации",
        "text": (
            "Фильтр Калмана (Kalman filter) — оптимальный рекурсивный оцениватель вектора состояния по зашумлённым "
            "измерениям в два шага: прогноз (predict) по динамической модели и коррекция (update) с коэффициентом "
            "усиления Калмана K. Для нелинейных задач (навигация КА, ИНС + GNSS, IMU + одометрия) применяют "
            "расширенный EKF (Extended Kalman Filter) и сигма-точечный UKF (Unscented Kalman Filter). "
            "Комплексирование датчиков (sensor fusion) объединяет IMU, GPS, лидар и камеры в устойчивую оценку позы."
        ),
    },
    {
        "id": "builtin-5",
        "status": "approved",
        "source": "Бортовой архив J (офлайн)",
        "url": "offline://aerospace-robotics/robot-kinematics-dynamics",
        "score": 1.0,
        "title": "Кинематика и динамика роботов (Robot kinematics & inverse kinematics)",
        "topic": "кинематика динамика роботов манипулятор robot kinematics inverse kinematics",
        "section": "Робототехника и планирование",
        "text": (
            "Прямая кинематика (forward kinematics, FK) вычисляет положение и ориентацию рабочего органа манипулятора "
            "по углам звеньев через однородные преобразования и параметры Денавита–Хартенберга (DH). Обратная кинематика "
            "(inverse kinematics, IK) находит обобщённые координаты q для целевой позы аналитически или численно через "
            "якобиан J (метод демпфированных наименьших квадратов DLS). Динамика робота: M(q)q̈ + C(q,q̇)q̇ + g(q) = τ."
        ),
    },
    {
        "id": "builtin-6",
        "status": "approved",
        "source": "Бортовой архив J (офлайн)",
        "url": "offline://aerospace-robotics/slam-lidar-odometry",
        "score": 1.0,
        "title": "Навигация SLAM, лидар и одометрия (SLAM navigation, LiDAR & odometry)",
        "topic": "навигация slam лидар одометрия lidar odometry",
        "section": "Системы управления и навигации",
        "text": (
            "SLAM (Simultaneous Localization and Mapping — одновременная локализация и картографирование) позволяет "
            "роботу строить карту неизвестной среды и оценивать свою позу без внешних маяков. Основные сенсоры: "
            "LiDAR (лазерное сканирование облаков точек с сопоставлением ICP/NDT), визуально-инерциальная одометрия "
            "(VIO с камерой и IMU) и колёсная одометрия. Замыкание петель (loop closure) и оптимизация графа поз "
            "(pose graph optimization) устраняют накопленный навигационный дрейф."
        ),
    },
    {
        "id": "builtin-7",
        "status": "approved",
        "source": "Бортовой архив J (офлайн)",
        "url": "offline://aerospace-robotics/spacecraft-attitude-control-adcs",
        "score": 1.0,
        "title": "Система ориентации и стабилизации КА (Spacecraft attitude control ADCS)",
        "topic": "система ориентации стабилизации ка adcs spacecraft attitude control",
        "section": "Системы управления и навигации",
        "text": (
            "ADCS (Attitude Determination and Control System — система ориентации и стабилизации космического аппарата) "
            "измеряет угловое положение и удерживает заданную ориентацию. Датчики: звёздные датчики (star trackers), "
            "солнечные датчики, магнитометры и гироскопы (IMU). Актуаторы: двигатели-маховики (reaction wheels), "
            "силовые гироскопы (CMG), магнитные катушки (magnetorquers для разгрузки маховиков) и сопла RCS. "
            "Вращение описывают кватернионами (quaternions), исключая вырождение углов Эйлера (gimbal lock)."
        ),
    },
    {
        "id": "builtin-8",
        "status": "approved",
        "source": "Бортовой архив J (офлайн)",
        "url": "offline://aerospace-robotics/autonomous-rendezvous-docking",
        "score": 1.0,
        "title": "Сближение и автономная стыковка (Autonomous rendezvous and docking)",
        "topic": "сближение автономная стыковка rendezvous docking",
        "section": "Баллистика и орбитальная механика",
        "text": (
            "Орбитальное сближение и стыковка (autonomous rendezvous and docking, AR&D) состоит из фазирования, "
            "дальнего сближения, относительной навигации в системе LVLH по уравнениям Клохесси–Уилтшира "
            "(Clohessy–Wiltshire / Hill equations) и финального причаливания внутри безопасного коридора подхода "
            "(approach corridor) с ограничением скорости. Для наведения применяют лидары, оптические мишени и радары; "
            "мягкий захват выполняет стыковочный механизм с демпфированием и стягиванием замков."
        ),
    },
    {
        "id": "builtin-9",
        "status": "approved",
        "source": "Бортовой архив J (офлайн)",
        "url": "offline://aerospace-robotics/telemetry-fdir-watchdog-safemode",
        "score": 1.0,
        "title": "Бортовая телеметрия и отказоустойчивость FDIR (Telemetry, watchdog & Safe Mode)",
        "topic": "бортовая телеметрия отказоустойчивость fdir watchdog safe mode",
        "section": "Бортовые системы и отказоустойчивость",
        "text": (
            "FDIR (Fault Detection, Isolation, and Recovery — обнаружение, локализация и парирование отказов) "
            "обеспечивает автономную живучесть роботов и КА при потере связи. Бортовая телеметрия (telemetry) "
            "контролирует напряжения, токи, температуры, угловые скорости и кворум датчиков. Сторожевой таймер "
            "(watchdog timer) перезапускает зависший процесс. При критическом сбое FDIR переводит аппарат в "
            "защищённое состояние Safe Mode: отключает второстепенную нагрузку и наводит батареи на Солнце."
        ),
    },
    {
        "id": "builtin-10",
        "status": "approved",
        "source": "Бортовой архив J (офлайн)",
        "url": "offline://aerospace-robotics/power-eps-thermal-control-tcs",
        "score": 1.0,
        "title": "Энергопитание и терморегуляция КА и роботов (Power EPS & thermal control TCS)",
        "topic": "энергопитание терморегуляция eps tcs power thermal control",
        "section": "Бортовые системы и отказоустойчивость",
        "text": (
            "EPS (Electrical Power Subsystem — система энергопитания) генерирует, накапливает и распределяет энергию: "
            "солнечные батареи с трекером точки максимальной мощности MPPT (Maximum Power Point Tracking), литий-ионные "
            "аккумуляторы (Li-Ion) или РИТЭГ (RTG) для дальнего космоса и токовые ограничители LCL. TCS (Thermal Control "
            "Subsystem — система терморегуляции) удерживает температурный баланс с помощью экранно-вакуумной изоляции "
            "ЭВТИ (MLI), радиаторов сброса тепла, тепловых трубок (heat pipes) и электронагревателей."
        ),
    },
    {
        "id": "builtin-11",
        "status": "approved",
        "source": "Бортовой архив J (офлайн)",
        "url": "offline://aerospace-robotics/onboard-data-buses-protocols",
        "score": 1.0,
        "title": "Бортовые шины данных и протоколы (CAN, SpaceWire, MIL-STD-1553, ROS 2, MAVLink)",
        "topic": "бортовые шины данных протоколы can spacewire mil-std-1553 ros 2 mavlink",
        "section": "Бортовые системы и отказоустойчивость",
        "text": (
            "Бортовые шины связывают датчики, приводы и вычислители. В роботах: CAN-шина (CAN / CAN FD с детерминированным "
            "арбитражом по ID и высокой помехоустойчивостью), ROS 2 (фреймворк поверх DDS с топиками pub/sub, сервисами "
            "и политиками QoS) и MAVLink (лёгкий бинарный протокол телеметрии и команд БПЛА). В космонавтике: "
            "MIL-STD-1553B (дублированная командно-ответная шина 1 Мбит/с) и SpaceWire (высокоскоростная пакетная сеть "
            "с маршрутизацией wormhole для полезной нагрузки)."
        ),
    },
    {
        "id": "builtin-12",
        "status": "approved",
        "source": "Бортовой архив J (офлайн)",
        "url": "offline://aerospace-robotics/uav-aerodynamics-flight-control",
        "score": 1.0,
        "title": "Аэродинамика и управление БПЛА (UAV aerodynamics & flight control)",
        "topic": "аэродинамика управление бпла дрон квадрокоптер uav aerodynamics flight control",
        "section": "Робототехника и планирование",
        "text": (
            "Беспилотные летательные аппараты (БПЛА, UAV) делятся на самолётные (fixed-wing — подъёмная сила крыла "
            "L = ½·ρ·v²·S·CL, большая дальность и крейсерская скорость), мультироторные (multirotor / квадрокоптер — "
            "управление тягой и моментами роторов по крену roll, тангажу pitch и рысканию yaw) и конвертопланы (VTOL). "
            "Автопилоты (PX4, ArduPilot) стабилизируют углы, удерживают высоту, ведут полёт по маршрутным точкам "
            "(waypoints) и компенсируют порывы ветра."
        ),
    },
    {
        "id": "builtin-13",
        "status": "approved",
        "source": "Бортовой архив J (офлайн)",
        "url": "offline://aerospace-robotics/radiation-hardening-tmr-ecc",
        "score": 1.0,
        "title": "Радиационная стойкость и экстремальные среды (Radiation hardening, TMR, ECC)",
        "topic": "радиационная стойкость экстремальные среды radiation hardening tmr ecc",
        "section": "Бортовые системы и отказоустойчивость",
        "text": (
            "Электроника космоса и экстремальных сред подвержена дозовой деградации TID (Total Ionizing Dose) и "
            "одиночным эффектам SEE (Single Event Effects): сбою памяти SEU (Single Event Upset) и тиристорному "
            "защёлкиванию SEL (latch-up). Защита включает радиационно-стойкие кристаллы (rad-hard, SOI), экранирование, "
            "тройное модульное резервирование TMR (Triple Modular Redundancy с голосованием 2-из-3), память с коррекцией "
            "ошибок ECC (Error-Correcting Coding, Хэмминг SECDED) и фоновую очистку памяти (scrubbing)."
        ),
    },
    {
        "id": "builtin-14",
        "status": "approved",
        "source": "Бортовой архив J (офлайн)",
        "url": "offline://aerospace-robotics/motion-planning-obstacle-avoidance",
        "score": 1.0,
        "title": "Планирование движения и обход препятствий (Motion planning A*, RRT, DWA)",
        "topic": "планирование движения обход препятствий motion planning obstacle avoidance astar rrt dwa",
        "section": "Робототехника и планирование",
        "text": (
            "Планирование траектории (motion planning) сочетает глобальный поиск пути и локальный обход препятствий "
            "(obstacle avoidance). На карте занятости (occupancy grid) применяют A* (оптимальный поиск с допустимой "
            "эвристикой) и D* Lite (инкрементальное перепланирование при изменении среды). В многомерных пространствах "
            "манипуляторов используют случайные деревья RRT и RRT* (Rapidly-exploring Random Tree). Для локального "
            "объезда в реальном времени служит динамическое окно DWA (Dynamic Window Approach) и потенциальные поля."
        ),
    },
]

_BUILTIN_ALIASES: dict[str, tuple[str, ...]] = {
    "builtin-1": (
        "Формула Циолковского", "уравнение Циолковского", "Циолковский",
        "Tsiolkovsky rocket equation", "Tsiolkovsky equation", "Tsiolkovsky",
        "rocket equation", "дельта-v", "delta-v", "удельный импульс", "specific impulse",
    ),
    "builtin-2": (
        "Орбитальная механика", "гомановский переход", "орбитальный манёвр",
        "первая космическая скорость", "вторая космическая скорость", "орбитальная скорость",
        "Orbital mechanics", "Hohmann transfer", "Hohmann", "orbital transfer",
        "escape velocity", "first cosmic velocity", "second cosmic velocity",
    ),
    "builtin-3": (
        "ПИД-регулятор", "ПИД регулятор", "ПИД",
        "PID controller", "PID control", "PID", "anti-windup",
    ),
    "builtin-4": (
        "Фильтр Калмана", "Калман", "комплексирование датчиков",
        "Kalman filter", "Kalman", "EKF", "UKF", "Extended Kalman Filter", "sensor fusion",
    ),
    "builtin-5": (
        "Кинематика роботов", "динамика роботов", "прямая кинематика", "обратная кинематика",
        "Robot kinematics", "inverse kinematics", "forward kinematics", "robot dynamics",
        "Denavit Hartenberg", "якобиан манипулятора",
    ),
    "builtin-6": (
        "SLAM", "навигация SLAM", "лидар", "лидары", "лидара", "одометрия", "визуальная одометрия",
        "SLAM navigation", "LiDAR", "odometry", "visual odometry", "loop closure",
    ),
    "builtin-7": (
        "ADCS", "ориентация и стабилизация", "ориентация спутника", "стабилизация КА",
        "двигатели маховики", "звёздный датчик", "кватернионы ориентации",
        "Spacecraft attitude control", "attitude control", "reaction wheels", "star tracker", "magnetorquer",
    ),
    "builtin-8": (
        "Сближение и стыковка", "автономная стыковка", "автоматическая стыковка",
        "орбитальное сближение", "стыковка", "Autonomous rendezvous and docking",
        "rendezvous and docking", "spacecraft docking", "docking", "rendezvous", "Clohessy Wiltshire",
    ),
    "builtin-9": (
        "FDIR", "бортовая телеметрия", "телеметрия", "отказоустойчивость", "сторожевой таймер",
        "Safe Mode", "безопасный режим", "telemetry", "watchdog timer",
        "fault detection isolation recovery",
    ),
    "builtin-10": (
        "Энергопитание КА", "терморегуляция", "терморегулирование", "EPS", "TCS", "MPPT",
        "солнечные панели", "тепловые трубки", "Power EPS", "thermal control",
        "electrical power subsystem", "heat pipes",
    ),
    "builtin-11": (
        "Бортовые шины данных", "протоколы робототехники", "CAN шина", "CAN-шина", "CAN bus",
        "ROS 2", "MIL-STD-1553", "onboard data buses", "data bus",
    ),
    "builtin-12": (
        "Аэродинамика БПЛА", "управление БПЛА", "БПЛА", "квадрокоптер",
        "UAV aerodynamics", "UAV", "flight control", "quadrotor", "PX4", "ArduPilot", "VTOL",
    ),
    "builtin-13": (
        "Радиационная стойкость", "тройное модульное резервирование", "TMR", "ECC",
        "память ECC", "одиночные сбои SEU", "Radiation hardening", "rad-hard",
        "Triple Modular Redundancy", "SEU",
    ),
    "builtin-14": (
        "Планирование движения", "обход препятствий", "планирование траектории",
        "RRT", "DWA", "Motion planning", "obstacle avoidance", "path planning",
        "Dynamic Window Approach", "occupancy grid",
    ),
}

_BUILTIN_EXACT_ALIASES: dict[str, tuple[tuple[str, str], ...]] = {
    "builtin-9": (("watchdog", "watchdog"),),
    "builtin-11": (
        ("spacewire", "SpaceWire"),
        ("mavlink", "MAVLink"),
        ("mil-std-1553", "MIL-STD-1553"),
        ("1553", "MIL-STD-1553"),
        ("ros 2", "ROS 2"),
        ("ros2", "ROS 2"),
        ("can bus", "CAN bus"),
        ("can шина", "CAN шина"),
        ("can-шина", "CAN шина"),
        ("шина can", "шина CAN"),
        ("протокол can", "протокол CAN"),
    ),
    "builtin-13": (("радиац", "радиация"), ("radiat", "radiation")),
    "builtin-14": (("a*", "алгоритм"), ("d*", "алгоритм")),
}

_DOMAIN_WORDS = (
    "циолковский орбита орбитальный гомановский пид калман калмана кинематика slam лидар лидара "
    "одометрия adcs ориентация стабилизация стыковка телеметрия fdir watchdog терморегуляция "
    "терморегулирование энергопитание spacewire mavlink ros can бпла uav радиация радиационная "
    "tmr ecc rrt dwa аэродинамика квадрокоптер актуатор гироскоп маховик двигатель импульс "
    "навигация траектория робот роботов космический спутник ракета дрон дрона шина протокол "
    "отказоустойчивость манипулятор датчиков дельта delta tsiolkovsky orbit orbital hohmann "
    "pid kalman ekf ukf kinematics lidar odometry attitude docking telemetry aerodynamics "
    "radiation drone rocket spacecraft thruster impulse propulsion actuator gyroscope "
    "navigation trajectory obstacle planning motion control thermal eps tcs mppt vtol px4 ardupilot "
    "bus mil 1553 1553b"
)
_GENERIC_STOP = set(stems("система контроль управление данные модель режим игра статус robot system control data"))
_AEROSPACE_DOMAIN_STEMS = (set(stems(_DOMAIN_WORDS)) - STOP_Q - STOP_WORDS - _GENERIC_STOP) | {
    "can", "ros", "pid", "пид", "fdir", "adcs", "slam", "tmr", "ecc", "rrt", "dwa", "uav",
    "бпла", "eps", "tcs", "ekf", "ukf", "vtol", "px4", "дрон", "шина", "шины", "шин",
    "bus", "mil", "1553", "1553b",
}


# ---------- утилиты ----------
def clean_text(raw: str) -> str:
    t = html.unescape(re.sub(r"<[^>]+>", " ", raw or ""))
    t = re.sub(r"\[[^\]]{1,12}\]", "", t)  # сноски вида [1]
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > MAX_TEXT:
        cut = t[:MAX_TEXT]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        t = cut[: end + 1] if end > 200 else cut.rstrip() + "…"
    return t


def safe_topic(text: str) -> str | None:
    """Тема для поиска уходит на внешний сайт, поэтому личное и подозрительное не пропускаем."""
    t = norm(text).strip(" ?!.,:;\"'«»")
    if not t or PRIVATE.search(t):
        return None
    words = [w for w in _WORDS.findall(t) if w[:5] not in STOP_Q]
    if not words or max(len(w) for w in words) < 3 or len(words) > 8:
        return None
    return " ".join(words)[:80]


def load_sources(path: Path | None = None) -> list[dict]:
    path = Path(path) if path else data_path("j_sources.json", "J_SOURCES")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        good = [s for s in data if isinstance(s, dict) and str(s.get("api", "")).startswith("https://")]
        if good:
            return good
    except (OSError, ValueError):
        pass
    try:  # создаём файл, чтобы список сайтов можно было править руками
        path.write_text(json.dumps(DEFAULT_SOURCES, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    return list(DEFAULT_SOURCES)


# ---------- безопасная загрузка ----------
class WebFetcher:
    def __init__(self, sources: list[dict], min_interval: float = 1.0, daily_cap: int = DAILY_CAP,
                 opener=None, contact: str = "") -> None:
        self.sources = sources
        self.min_interval = min_interval
        self.daily_cap = daily_cap
        self._open = opener or urllib.request.urlopen
        self._custom_opener = opener is not None
        self.online = not is_offline_mode()
        self.last_fail_ts = 0.0
        try:
            self.retry_cooldown = float(os.environ.get("J_NET_RETRY_SEC", "30") or 30)
        except ValueError:
            self.retry_cooldown = 30.0
        self._last = 0.0
        self._day = ""
        self._count = 0
        self.ua = f"J-Study/0.1 (hobby bot; {contact or 'contact not set'})"

    def is_online(self) -> bool:
        if is_offline_mode():
            return False
        if self._custom_opener:
            return True
        if not self.online and (time.time() - self.last_fail_ts < self.retry_cooldown):
            return False
        return True

    def _allow(self) -> bool:
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day, self._count = today, 0
        if self._count >= self.daily_cap:
            return False
        self._count += 1
        return True

    def _get(self, api: str, params: dict) -> dict | None:
        if not api.startswith("https://") or not self.is_online() or not self._allow():
            return None
        wait = self.min_interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        host = urllib.parse.urlparse(api).hostname
        url = api + "?" + urllib.parse.urlencode(params)
        try:
            timeout = float(os.environ.get("J_NET_TIMEOUT", "10") or 10)
        except ValueError:
            timeout = 10.0
        try:
            req = urllib.request.Request(url, headers={"User-Agent": self.ua, "Accept": "application/json"})
            with self._open(req, timeout=timeout) as resp:
                if urllib.parse.urlparse(resp.geturl()).hostname != host:  # редирект на чужой сайт — не доверяем
                    return None
                raw = resp.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                return None
            data = json.loads(raw.decode("utf-8", "replace"))
            self.online = True
            self.last_fail_ts = 0.0
            return data
        except Exception:
            self.online = False
            self.last_fail_ts = time.time()
            return None

    @staticmethod
    def _page_url(api: str, title: str) -> str:
        return api.rsplit("api.php", 1)[0] + "index.php?title=" + urllib.parse.quote(title.replace(" ", "_"))

    def search(self, query: str, source: dict, limit: int = 3) -> list[dict]:
        api = source["api"]
        data = self._get(api, {
            "action": "query", "format": "json", "formatversion": "2", "generator": "search", "gsrsearch": query,
            "gsrlimit": str(limit), "gsrnamespace": "0", "prop": "extracts|info", "exintro": "1",
            "explaintext": "1", "exsentences": "4", "exlimit": "max", "inprop": "url", "redirects": "1",
        })
        pages = ((data or {}).get("query") or {}).get("pages") or []
        if isinstance(pages, dict):
            pages = list(pages.values())
        out = []
        for pg in sorted(pages, key=lambda p: p.get("index", 99)):
            if pg.get("title") and pg.get("extract"):
                out.append({"title": pg["title"], "text": clean_text(pg["extract"]),
                            "url": pg.get("fullurl") or self._page_url(api, pg["title"])})
        if out:
            return out
        data = self._get(api, {  # запасной путь: сниппеты поиска (если на вики нет расширения extracts)
            "action": "query", "list": "search", "srsearch": query, "srlimit": str(limit),
            "srnamespace": "0", "format": "json", "formatversion": "2",
        })
        for r in ((data or {}).get("query") or {}).get("search") or []:
            if r.get("title"):
                out.append({"title": r["title"], "text": clean_text(r.get("snippet", "")),
                            "url": self._page_url(api, r["title"])})
        return out


# ---------- знания ----------
class Knowledge:
    def __init__(self, path: Path | None = None, sources: list[dict] | None = None,
                 fetcher: WebFetcher | None = None) -> None:
        self.path = Path(path) if path else data_path("j_knowledge.json", "J_KNOWLEDGE")
        self.lock = threading.RLock()
        self.sources = sources if sources is not None else load_sources()
        self.fetcher = fetcher or WebFetcher(self.sources, contact=os.environ.get("J_CONTACT", ""))
        self.min_score = MIN_SCORE  # J подстраивает его сама по тому, как часто её находки одобряют (j_self.py)
        self.words: dict[str, Counter] = defaultdict(Counter)
        self.data = {"items": [], "queue": [], "done": [], "rejected": [], "deferred": [], "next_id": 1}
        self._load()
        self._recluster()

    # --- хранение ---
    def _load(self) -> None:
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            self.data.update({k: v for k, v in loaded.items() if k in self.data})
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            try:
                self.path.replace(self.path.with_suffix(".bak"))
            except OSError:
                pass

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    # --- темы ---
    def queue_topic(self, text: str) -> tuple[bool, str]:
        topic = safe_topic(text)
        if not topic:
            return False, "invalid"
        with self.lock:
            if topic in self.data["queue"] or topic in self.data["done"]:
                return False, "known"
            if len(self.data["queue"]) >= MAX_QUEUE:
                return False, "full"
            self.data["queue"].append(topic)
            self._save()
        return True, topic

    def study_next(self) -> dict | None:
        with self.lock:
            if self.data.get("deferred") and self.fetcher.is_online():
                remaining = []
                for t in self.data["deferred"]:
                    if t in self.data["queue"] or t in self.data["done"]:
                        continue
                    if len(self.data["queue"]) < MAX_QUEUE:
                        self.data["queue"].append(t)
                    else:
                        remaining.append(t)
                self.data["deferred"] = remaining
                self._save()
            if not self.data["queue"] or self._pending_count() >= MAX_PENDING:
                return None
            topic = self.data["queue"].pop(0)
        return self.study_topic(topic)

    def _pending_count(self) -> int:
        return sum(1 for i in self.data["items"] if i["status"] == "pending")

    def _append_item(self, topic: str, item_status: str, title: str, text: str, url: str,
                     source: str, license_str: str = "", score: float = 1.0, section: str = "") -> None:
        self.data["items"].append({
            "id": self.data["next_id"], "status": item_status, "topic": topic, "title": title,
            "text": text, "url": url, "source": source, "license": license_str,
            "fetched": time.strftime("%Y-%m-%d"), "score": score, "section": section,
        })
        self.data["next_id"] += 1

    # --- изучение одной темы ---
    def study_topic(self, topic: str) -> dict:
        stats = {"topic": topic, "found": 0, "added": 0, "skipped": 0}
        candidates = []
        for src in self.sources:  # сеть — вне блокировки
            for c in self.fetcher.search(topic, src):
                c["source"] = src
                candidates.append(c)
        offline_fallback = (
            not candidates
            and not getattr(self.fetcher, "_custom_opener", False)
            and (not self.fetcher.is_online() or is_offline_mode())
        )
        auto_approve = _truthy_env("J_AUTO_APPROVE", "0") and not getattr(self.fetcher, "_custom_opener", False)
        with self.lock:
            if offline_fallback:
                if os.environ.get("J_BUILTIN_KB", "1") != "0":
                    hit = self.search_builtin(topic, threshold=0.45)
                    if hit:
                        stats["found"] += 1
                        item_status = (
                            "approved"
                            if (_truthy_env("J_AUTO_APPROVE", "0") or os.environ.get("J_AUTONOMOUS", "1") != "0")
                            else "pending"
                        )
                        too_many_pending = item_status == "pending" and self._pending_count() >= MAX_PENDING
                        if (too_many_pending or self._is_duplicate(hit)
                                or self._hash(hit["url"]) in self.data["rejected"]):
                            stats["skipped"] += 1
                        else:
                            self._append_item(
                                topic, item_status, hit["title"], hit["text"], hit["url"],
                                hit["source"], hit.get("license", ""), hit.get("score", 1.0),
                                hit.get("section", ""),
                            )
                            stats["added"] += 1
                if stats["added"] == 0:
                    if topic not in self.data["done"]:
                        deferred = self.data.setdefault("deferred", [])
                        if topic not in deferred:
                            self.data["deferred"] = (deferred + [topic])[-50:]
                    self._recluster()
                    self._save()
                    return stats
            else:
                scored = []
                for c in candidates:
                    stats["found"] += 1
                    blob = c["title"] + " " + c["text"]
                    bad = (INJECTION.search(blob) or DISAMBIG.search(c["text"]) or len(c["text"]) < 60
                           or self._hash(c["url"]) in self.data["rejected"])
                    score = 0.0 if bad else self._score(topic, c)
                    if score < self.min_score:
                        stats["skipped"] += 1
                        continue
                    scored.append((score, c))
                scored.sort(key=lambda x: x[0], reverse=True)
                item_status = "approved" if auto_approve else "pending"
                for score, c in scored[:PER_TOPIC]:
                    if (item_status == "pending" and self._pending_count() >= MAX_PENDING) or self._is_duplicate(c):
                        stats["skipped"] += 1
                        continue
                    self._append_item(
                        topic, item_status, c["title"], c["text"], c["url"],
                        c["source"]["name"], c["source"].get("license", ""), round(score, 2), "",
                    )
                    stats["added"] += 1
                if topic in self.data.get("deferred", []):
                    self.data["deferred"] = [t for t in self.data["deferred"] if t != topic]
            if topic not in self.data["done"]:
                self.data["done"] = (self.data["done"] + [topic])[-200:]
            self._recluster()
            self._save()
        return stats

    @staticmethod
    def _hash(url: str) -> str:
        return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]

    def _score(self, topic: str, c: dict) -> float:
        q = {t for t in stems(topic) if t not in STOP_Q}
        title = set(stems(re.sub(r"\(.*?\)", "", c["title"])))
        body = set(stems(c["text"]))
        rel = max(len(q & title) / len(q), 0.7 * len(q & body) / len(q)) if q else 0.0
        n = len(c["text"])
        quality = 1.0 if 120 <= n <= MAX_TEXT + 5 else 0.5 if n >= 60 else 0.1
        return 0.5 * rel + 0.25 * quality + 0.25 * float(c["source"].get("trust", 0.5))

    def _is_duplicate(self, c: dict) -> bool:
        h = self._hash(c["url"])
        a = set(stems(c["text"]))
        for it in self.data["items"]:
            if self._hash(it["url"]) == h:
                return True
            b = set(stems(it["text"]))
            if a and b and len(a & b) / len(a | b) > 0.8:
                return True
        return False

    # --- проверка владельцем ---
    def pending(self, limit: int = 5) -> list[dict]:
        with self.lock:
            return [dict(i) for i in self.data["items"] if i["status"] == "pending"][:limit]

    def approve(self, ids: list[int] | None = None) -> int:
        with self.lock:
            n = 0
            for it in self.data["items"]:
                if it["status"] == "pending" and (ids is None or it["id"] in ids):
                    it["status"] = "approved"
                    n += 1
            self._save()
            return n

    def reject(self, ids: list[int] | None = None) -> int:
        """ids=None — отклонить все непроверенные. Отклонённый источник больше не вернётся."""
        with self.lock:
            keep, n = [], 0
            for it in self.data["items"]:
                hit = (ids is None and it["status"] == "pending") or (ids is not None and it["id"] in ids)
                if hit:
                    n += 1
                    self.data["rejected"] = (self.data["rejected"] + [self._hash(it["url"])])[-500:]
                else:
                    keep.append(it)
            self.data["items"] = keep
            self._recluster()
            self._save()
            return n

    # --- использование знаний ---
    @staticmethod
    def _query_sim(qs: set[str], title_cands: list[str], body_text: str) -> float:
        bs = set(stems(body_text))
        jac_b = len(qs & bs) / len(qs | bs) if bs else 0.0
        best_sim = 1.2 * jac_b
        for cand in title_cands:
            ts = set(stems(cand))
            if not ts:
                continue
            contain = len(qs & ts) / len(ts)
            jac_t = len(qs & ts) / len(qs | ts)
            cand_sim = max(0.9 if contain >= 0.99 else 0.0, jac_t, 1.2 * jac_b)
            if cand_sim > best_sim:
                best_sim = cand_sim
        return best_sim

    def search_builtin(self, query: str, threshold: float = 0.5) -> dict | None:
        qs = {t for t in stems(query) if t not in STOP_Q}
        if not qs or not (qs & _AEROSPACE_DOMAIN_STEMS):
            return None
        q_norm = norm(query)
        # Исключаем ложное совпадение 5-буквенных стемов «радиан»/«radian» с «радиация»/«radiation»
        if qs <= {"радиа", "radia"} and "радиац" not in q_norm and "radiat" not in q_norm:
            return None
        best = None
        for it in BUILTIN_AEROSPACE_KB:
            cands = [re.sub(r"\(.*?\)", "", it["title"])]
            cands.extend(re.findall(r"\(([^)]+)\)", it["title"]))
            cands.extend(_BUILTIN_ALIASES.get(it["id"], ()))
            for exact_w, alias_phrase in _BUILTIN_EXACT_ALIASES.get(it["id"], ()):
                if exact_w in q_norm:
                    cands.append(alias_phrase)
            sim = self._query_sim(qs, cands, it["text"])
            if sim >= threshold and (best is None or sim > best["sim"]):
                best = {**it, "sim": round(sim, 2)}
        return best

    def answer(self, query: str, threshold: float = 0.5) -> dict | None:
        qs = {t for t in stems(query) if t not in STOP_Q}
        if not qs:
            return None
        best = None
        with self.lock:
            for it in self.data["items"]:
                if it["status"] != "approved":
                    continue
                sim = self._query_sim(qs, [re.sub(r"\(.*?\)", "", it["title"])], it["text"])
                if sim >= threshold and (best is None or sim > best["sim"]):
                    best = {**it, "sim": round(sim, 2)}
        if best is None and os.environ.get("J_BUILTIN_KB", "1") != "0":
            return self.search_builtin(query, threshold=threshold)
        return best

    # --- разделы: обучение без учителя ---
    def _recluster(self) -> None:
        items = self.data["items"]
        if not items:
            return
        docs = []
        for it in items:
            blob = it["title"] + " " + it["text"]
            for w in _WORDS.findall(norm(blob)):
                self.words[w[:5]][w] += 1
            docs.append(Counter(t for t in stems(blob) if t not in STOP_Q and t not in STOP_WORDS and len(t) > 2))
        n = len(docs)
        df = Counter()
        for d in docs:
            df.update(d.keys())
        idf = {t: math.log((n + 1) / (c + 1)) + 1 for t, c in df.items()}
        clusters: list[dict] = []
        for idx, d in enumerate(docs):
            v = {t: (1 + math.log(c)) * idf[t] for t, c in d.items()}
            norm_v = math.sqrt(sum(x * x for x in v.values())) or 1.0
            v = {t: x / norm_v for t, x in v.items()}
            best, best_sim = None, 0.0
            for cl in clusters:
                cn = math.sqrt(sum(x * x for x in cl["c"].values())) or 1.0
                sim = sum(x * cl["c"].get(t, 0.0) for t, x in v.items()) / cn
                if sim > best_sim:
                    best, best_sim = cl, sim
            if best and best_sim >= CLUSTER_SIM:
                best["members"].append(idx)
                for t, x in v.items():
                    best["c"][t] = best["c"].get(t, 0.0) + x
            else:
                clusters.append({"c": dict(v), "members": [idx]})
        for cl in clusters:
            top = sorted(cl["c"], key=cl["c"].get, reverse=True)[:3]
            name = ", ".join(self._word(t) for t in top)
            for i in cl["members"]:
                items[i]["section"] = name

    def _word(self, stem: str) -> str:
        c = self.words.get(stem)
        return c.most_common(1)[0][0] if c else stem

    def sections(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        with self.lock:
            for it in self.data["items"]:
                s = out.setdefault(it["section"] or "—", {"approved": 0, "pending": 0})
                s[it["status"]] += 1
        return out

    def status(self) -> dict:
        with self.lock:
            return {
                "queue": list(self.data["queue"]), "pending": self._pending_count(),
                "approved": sum(1 for i in self.data["items"] if i["status"] == "approved"),
                "studied": len(self.data["done"]),
                "builtin": len(BUILTIN_AEROSPACE_KB),
                "online": getattr(self.fetcher, "is_online", lambda: True)(),
                "deferred": len(self.data.get("deferred", [])),
            }
