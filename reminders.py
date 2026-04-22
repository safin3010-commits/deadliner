"""
Система пользовательских напоминаний.
Хранит в data/reminders.json
"""
import json, os, datetime
from config import UFA_TZ

REMINDERS_FILE = "data/reminders.json"


def _load() -> list:
    try:
        with open(REMINDERS_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _save(data: list):
    os.makedirs("data", exist_ok=True)
    with open(REMINDERS_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def add_reminder(task_id: str, task_title: str, interval_minutes: int, times: int, start_at: str = None) -> dict:
    reminders = _load()
    now = datetime.datetime.now(tz=UFA_TZ)
    if start_at:
        try:
            first_fire = datetime.datetime.fromisoformat(start_at)
            if first_fire.tzinfo is None:
                first_fire = first_fire.replace(tzinfo=UFA_TZ)
        except Exception:
            first_fire = now + datetime.timedelta(minutes=interval_minutes)
    else:
        first_fire = now + datetime.timedelta(minutes=interval_minutes)
    reminder = {
        "id": f"rem_{int(now.timestamp())}",
        "task_id": str(task_id),
        "task_title": task_title,
        "interval_minutes": interval_minutes,
        "times_left": times,
        "next_at": first_fire.isoformat(),
    }
    reminders.append(reminder)
    _save(reminders)
    return reminder


def get_due_reminders() -> list:
    reminders = _load()
    now = datetime.datetime.now(tz=UFA_TZ)
    due = []
    for r in reminders:
        try:
            next_at = datetime.datetime.fromisoformat(r["next_at"])
            if now >= next_at and r.get("times_left", 0) > 0:
                due.append(r)
        except Exception:
            continue
    return due


def mark_sent(reminder_id: str):
    reminders = _load()
    now = datetime.datetime.now(tz=UFA_TZ)
    updated = []
    for r in reminders:
        if r["id"] == reminder_id:
            r["times_left"] -= 1
            if r["times_left"] > 0:
                r["next_at"] = (now + datetime.timedelta(minutes=r["interval_minutes"])).isoformat()
                updated.append(r)
        else:
            updated.append(r)
    _save(updated)


def get_all_reminders() -> list:
    return [r for r in _load() if r.get("times_left", 0) > 0]


def delete_reminder(reminder_id: str):
    reminders = [r for r in _load() if r["id"] != reminder_id]
    _save(reminders)


def format_interval(minutes: int) -> str:
    if minutes < 60:
        return f"каждые {minutes} мин"
    elif minutes == 60:
        return "каждый час"
    elif minutes % 60 == 0:
        return f"каждые {minutes // 60} ч"
    return f"каждые {minutes} мин"
