#!/usr/bin/env python3
"""
Отдельный, полностью не-AI процесс — раз в 30 минут обновляет
data/yac_schedule.json (ссылки на вебинары my.mts-link.ru/netology.ru и
флаг LXP от yetanothercalendar.ru) и пересобирает окно на столе.

Ссылки окно подставляет само при рендере (scripts/mentor_dashboard.py), так
что свежая ссылка появляется на столе без вызова Claude. Окно и так следит за
data/yac_schedule.json и пересоберётся само — явный рендер здесь страховка на
случай, если окно не запущено (файл пишется только при реальных изменениях).
"""
import datetime
import os
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)
sys.path.insert(0, os.path.join(PROJECT_DIR, "scripts"))
# launchd запускает нас с cwd=/ — переходим в проект, чтобы относительные
# "data/..." пути резолвились туда же, куда смотрит сам бот.
os.chdir(PROJECT_DIR)

import mentor_checkin as mc
from mentor_hourly_watch import refresh_yac_schedule


def log(msg: str):
    with open(mc.LOG_FILE, "a") as f:
        f.write(f"[{datetime.datetime.now().isoformat()}] [yac_links] {msg}\n")


def main():
    refresh_yac_schedule()
    try:
        from parsers.yandex_group_calendar import refresh as refresh_group_calendar
        n = refresh_group_calendar()
        log(f"календарь группы: {n} событий")
    except Exception as e:
        log(f"календарь группы: ошибка {e!r}")
    try:
        from mentor_dashboard import render_feed
        if render_feed():
            log("окно на столе пересобрано (новые ссылки/данные)")
    except Exception as e:
        log(f"ошибка рендера окна: {e!r}")


if __name__ == "__main__":
    main()
