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
    # Не создаём дубль — если напоминание на эту задачу уже есть
    for existing in reminders:
        if str(existing.get("task_id")) == str(task_id) and existing.get("times_left", 0) > 0:
            print(f"Reminder: дубль для task_id={task_id} пропущен")
            return existing
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
        "id": f"rem_{int(now.timestamp())}_{__import__('random').randint(1000,9999)}",
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
                interval = r.get("interval_minutes", 0)
                if interval > 0:
                    r["next_at"] = (now + datetime.timedelta(minutes=interval)).isoformat()
                else:
                    # interval=0 — однократное, не зацикливаем
                    r["times_left"] = 0
                    # не добавляем в updated — напоминание удалится
                    continue
                updated.append(r)
        else:
            updated.append(r)
    _save(updated)


def get_all_reminders() -> list:
    return [r for r in _load() if r.get("times_left", 0) > 0]


def delete_reminder(reminder_id: str):
    reminders = [r for r in _load() if r["id"] != reminder_id]
    _save(reminders)


def format_interval(minutes: int, times: int = 0) -> str:
    if times == 1:
        return "однократно"
    if minutes == 0:
        return "однократно"
    if minutes < 60:
        return f"каждые {minutes} мин"
    elif minutes == 60:
        return "каждый час"
    elif minutes % 60 == 0:
        return f"каждые {minutes // 60} ч"
    return f"каждые {minutes} мин"


def save_last_message_id(reminder_id: str, message_id: int):
    """Сохраняем message_id последнего отправленного напоминания."""
    reminders = _load()
    for r in reminders:
        if str(r["id"]) == str(reminder_id):
            r["last_message_id"] = message_id
            break
    _save(reminders)
