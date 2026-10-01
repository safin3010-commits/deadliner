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


def _load_raw() -> dict:
    try:
        with open(STREAK_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data: dict):
    os.makedirs("data", exist_ok=True)
    tmp = STREAK_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STREAK_FILE)


def active_days() -> set:
    """Дни, в которые пользователь сам закрыл хотя бы одну задачу
    (manually_done + done_at) — в боте, галочкой на столе, текстом."""
    from storage import get_tasks
    days = set()
    for t in get_tasks():
        if t.get("manually_done") and t.get("done_at"):
            try:
                days.add(datetime.datetime.fromisoformat(t["done_at"]).astimezone(UFA_TZ).date())
            except ValueError:
                pass
    return days


def compute_streak() -> dict:
    """Стрик считается из дат закрытия задач, а не хранится счётчиком.
    Раньше mark_active_today() никто не вызывал, и streak.json застыл на
    16.04 — сообщения наставника и /streak показывали неправду. Сегодняшний
    день без закрытий стрик ещё не обрывает (день не кончился)."""
    days = active_days()
    today = datetime.datetime.now(tz=UFA_TZ).date()
    cur = 0
    d = today if today in days else today - datetime.timedelta(days=1)
    while d in days:
        cur += 1
        d -= datetime.timedelta(days=1)
    best = run = 0
    prev = None
    for day in sorted(days):
        run = run + 1 if prev and (day - prev).days == 1 else 1
        best = max(best, run)
        prev = day
    last = max(days).isoformat() if days else ""
    return {"streak": cur, "max_streak": best, "last_active": last}


def _load() -> dict:
    data = _load_raw()
    computed = compute_streak()
    merged = {**data, **computed}
    if any(data.get(k) != computed[k] for k in computed):
        # Держим streak.json в актуальном виде для тех, кто читает файл.
        _save(merged)
    return merged


def get_streak() -> int:
    """Возвращает текущий стрик."""
    return _load().get("streak", 0)


def get_max_streak() -> int:
    return _load().get("max_streak", 0)


def mark_active_today() -> dict:
    """Совместимость: стрик теперь вычисляется сам из закрытых задач."""
    before = _load_raw().get("max_streak", 0)
    data = _load()
    return {"streak": data["streak"], "is_new_record": data["streak"] > before, "continued": data["streak"] > 1}


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
    data = _load_raw()
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
    done_total = len([t for t in tasks if t.get("manually_done")])

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
