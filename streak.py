"""
Система стриков продуктивности.
Стрик растёт если за день выполнена хотя бы одна задача.
Хранится в data/streak.json
"""

import json
import os
import datetime
from config import UFA_TZ

STREAK_FILE = "data/streak.json"


def _load() -> dict:
    try:
        with open(STREAK_FILE) as f:
            return json.load(f)
    except Exception:
        return {"streak": 0, "last_active": "", "max_streak": 0, "evening_reported": ""}


def _save(data: dict):
    os.makedirs("data", exist_ok=True)
    with open(STREAK_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_streak() -> int:
    """Возвращает текущий стрик."""
    return _load().get("streak", 0)


def get_max_streak() -> int:
    return _load().get("max_streak", 0)


def mark_active_today() -> dict:
    """
    Отмечаем сегодняшний день как активный (выполнена хоть одна задача).
    Возвращает {"streak": N, "is_new_record": bool, "continued": bool}
    """
    today = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
    yesterday = (datetime.datetime.now(tz=UFA_TZ).date() - datetime.timedelta(days=1)).isoformat()

    data = _load()
    last = data.get("last_active", "")

    if last == today:
        # Уже отмечено сегодня
        return {"streak": data["streak"], "is_new_record": False, "continued": False}

    if last == yesterday:
        # Продолжаем стрик
        data["streak"] += 1
        continued = True
    else:
        # Стрик прерван или первый день
        data["streak"] = 1
        continued = False

    data["last_active"] = today
    old_max = data.get("max_streak", 0)
    if data["streak"] > old_max:
        data["max_streak"] = data["streak"]

    _save(data)
    return {
        "streak": data["streak"],
        "is_new_record": data["streak"] > old_max,
        "continued": continued,
    }


def check_streak_at_risk() -> bool:
    """
    Проверяем под угрозой ли стрик — если сегодня ещё не было активности
    и сейчас вечер (после 20:00).
    """
    now = datetime.datetime.now(tz=UFA_TZ)
    if now.hour < 20:
        return False

    today = now.date().isoformat()
    data = _load()
    return data.get("last_active", "") != today and data.get("streak", 0) > 0


def was_evening_reported() -> bool:
    """Отчитался ли пользователь сегодня вечером."""
    today = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
    return _load().get("evening_reported", "") == today


def mark_evening_reported():
    """Отмечаем что вечерний отчёт сделан."""
    today = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
    data = _load()
    data["evening_reported"] = today
    _save(data)


def get_weekly_stats() -> dict:
    """Статистика за последние 7 дней."""
    from storage import get_tasks
    tasks = get_tasks()
    now = datetime.datetime.now(tz=UFA_TZ)

    done_this_week = 0
    total = len([t for t in tasks if not t.get("done")])

    # Считаем выполненные за неделю — упрощённо через общий счётчик done
    # В идеале нужна дата выполнения, но её нет в текущей структуре
    done_total = len([t for t in tasks if t.get("done")])

    data = _load()
    return {
        "streak": data.get("streak", 0),
        "max_streak": data.get("max_streak", 0),
        "done_total": done_total,
        "pending_total": total,
    }


def streak_emoji(streak: int) -> str:
    if streak == 0:
        return "😴"
    elif streak < 3:
        return "🌱"
    elif streak < 7:
        return "🔥"
    elif streak < 14:
        return "⚡"
    elif streak < 30:
        return "💪"
    else:
        return "🏆"
