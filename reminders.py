"""
Система пользовательских напоминаний.
Хранит в data/reminders.json
"""
import json, os, datetime
from config import UFA_TZ
from data_lock import file_lock, atomic_write_json

REMINDERS_FILE = "data/reminders.json"


def _load() -> list:
    try:
        with open(REMINDERS_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _save(data: list):
    os.makedirs("data", exist_ok=True)
    atomic_write_json(REMINDERS_FILE, data)


def add_reminder(task_id: str, task_title: str, interval_minutes: int, times: int, start_at: str = None) -> dict:
    # file_lock — reminders.json читается-пишется и из хендлеров бота (эта
    # функция, delete_reminder), и из джоба check_user_reminders в scheduler.py
    # (раз в 3 минуты, mark_sent/delete_reminder/save_last_message_id) —
    # независимо, без общего лока конкурентный read-modify-write мог терять
    # чужую правку. atomic_write_json — раньше запись шла напрямую в
    # REMINDERS_FILE без временного файла: падение процесса посреди записи
    # оставляло битый JSON и обнуляло ВСЕ напоминания разом.
    with file_lock(REMINDERS_FILE):
        reminders = _load()
        now = datetime.datetime.now(tz=UFA_TZ)
        # Не создаём дубль — если напоминание на эту задачу уже есть
        for existing in reminders:
            if str(existing.get("task_id")) == str(task_id) and existing.get("times_left", 0) > 0:
                print(f"Reminder: дубль для task_id={task_id} пропущен")
                return existing
        # Отработавшее напоминание той же задачи заменяем новым, а не копим.
        reminders = [
            r for r in reminders
            if not (str(r.get("task_id")) == str(task_id) and r.get("times_left", 0) <= 0)
        ]
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
    """Отметить срабатывание. Отработавшее напоминание (повторы кончились или
    оно было разовым) НЕ удаляется: остаётся с times_left=0 и exhausted=True —
    больше не звонит, но висит в боте и на столе, пока пользователь сам его
    не закроет (так попросил пользователь 2026-10-01: "напоминания не должны
    уходить по сроку, только если я сам закрою")."""
    with file_lock(REMINDERS_FILE):
        reminders = _load()
        now = datetime.datetime.now(tz=UFA_TZ)
        for r in reminders:
            if r["id"] != reminder_id:
                continue
            r["times_left"] = max(r.get("times_left", 1) - 1, 0)
            r["last_fired_at"] = now.isoformat()
            interval = r.get("interval_minutes", 0)
            if r["times_left"] > 0 and interval > 0:
                r["next_at"] = (now + datetime.timedelta(minutes=interval)).isoformat()
            else:
                # interval=0 — однократное, не зацикливаем
                r["times_left"] = 0
                r["exhausted"] = True
        _save(reminders)


def get_all_reminders() -> list:
    """Все незакрытые напоминания — и звонящие, и отработавшие (exhausted)."""
    return _load()


def get_firing_reminders() -> list:
    """Только те, что ещё будут срабатывать."""
    return [r for r in _load() if r.get("times_left", 0) > 0]


def delete_reminder(reminder_id: str):
    with file_lock(REMINDERS_FILE):
        reminders = [r for r in _load() if r["id"] != reminder_id]
        _save(reminders)


def format_times_left(times_left: int) -> str:
    """Единое отображение остатка повторов — используется в списке активных
    напоминаний, в кнопках и в самом сообщении напоминания. times_left>=9999
    — конвенция проекта для "бессрочно, пока не остановят" (см. add_reminder/
    scheduler.py _is_daily) — раньше в разных местах либо показывали сырое
    число (страшное "×9998"), либо жёстко писали "каждый день", даже если
    интервал был не суточный."""
    if times_left >= 9999:
        return "бессрочно"
    if times_left <= 0:
        return "отработало"
    return f"×{times_left}"


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
    with file_lock(REMINDERS_FILE):
        reminders = _load()
        for r in reminders:
            if str(r["id"]) == str(reminder_id):
                r["last_message_id"] = message_id
                break
        _save(reminders)
