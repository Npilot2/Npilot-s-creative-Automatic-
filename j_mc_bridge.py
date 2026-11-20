#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Историческое имя: мост теперь часть ядра J (j_core.py), а данные игр — в реестре j_games.py.
Файл оставлен, чтобы старые команды запуска (python j_mc_bridge.py) и инструкции продолжали работать.
"""
from j_bridge import Bridge, SafeDict, make_handler  # noqa: F401  (совместимость старых импортов)
from j_core import main, prepare  # noqa: F401
from j_games import GAMES, get_game  # noqa: F401

if __name__ == "__main__":
    raise SystemExit(main())
