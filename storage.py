import json
import os
from typing import Any
from config import TASKS_FILE, SEEN_MESSAGES_FILE, TOKENS_FILE, DATA_DIR
from data_lock import file_lock


QUIZ_ACTIVE_FILE = "data/quiz_active.json"

def set_quiz_active(active: bool):
    """Включаем/выключаем режим квиза — блокирует LMS парсинг."""
    import os
    os.makedirs("data", exist_ok=True)
    with open(QUIZ_ACTIVE_FILE, "w") as f:
        import json
        json.dump({"active": active}, f)

def is_quiz_active() -> bool:
    """Проверяем активен ли квиз."""
    try:
        import json
        with open(QUIZ_ACTIVE_FILE) as f:
            return json.load(f).get("active", False)
    except Exception:
        return False


def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)

def read_json(filepath: str) -> Any:
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return None
            return json.loads(content)
    except (FileNotFoundError, json.JSONDecodeError):
        return None

def write_json(filepath: str, data: Any) -> None:
    ensure_data_dir()
    tmp_path = f"{filepath}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp_path, filepath)

def get_tasks() -> list:
    return read_json(TASKS_FILE) or []

def save_tasks(tasks: list) -> None:
    write_json(TASKS_FILE, tasks)

def add_task(title: str, deadline: str | None, source: str, event_date: str | None = None) -> dict:
    # file_lock — иначе два почти одновременных add_task (например ручное
    # добавление в боте + фоновая sync_all_tasks) могут прочитать один и
    # тот же tasks.json ДО того, как другой допишет свою задачу, и один
    # из результатов потеряется при перезаписи.
    with file_lock(TASKS_FILE):
        tasks = read_json(TASKS_FILE) or []
        # Берём максимальный числовой ID чтобы избежать дублей
        max_id = 0
        for t in tasks:
            try:
                tid = int(t["id"])
                if tid > max_id:
                    max_id = tid
            except (ValueError, TypeError):
                pass
        # Единый стиль: первая буква заглавная, остальное как есть
        if title:
            title = title[0].upper() + title[1:]
        task = {
            "id": max_id + 1,
            "title": title,
            "deadline": deadline,
            "source": source,
            "done": False,
        }
        if event_date:
            # Для reminder_only: deadline = момент напоминания, event_date —
            # дата самого события (окно наставника показывает именно её).
            task["event_date"] = event_date
        tasks.append(task)
        write_json(TASKS_FILE, tasks)
    return task

def mark_task_done(task_id, manually: bool = False) -> bool:
    """Отмечаем задачу выполненной. task_id может быть int или str."""
    import datetime
    from config import UFA_TZ
    with file_lock(TASKS_FILE):
        tasks = read_json(TASKS_FILE) or []
        for task in tasks:
            if str(task.get("id")) == str(task_id):
                task["done"] = True
                task["done_at"] = datetime.datetime.now(tz=UFA_TZ).isoformat()
                if manually:
                    task["manually_done"] = True
                write_json(TASKS_FILE, tasks)
                return True
    return False


def set_task_deadline(task_id, deadline_iso: str | None) -> dict | None:
    """Пользователь сам поменял срок задачи (кнопка «изменить дедлайн» или
    «перенеси срок» текстом). Ставим флаг deadline_overridden: синхронизация
    LMS/Нетологии больше не перезаписывает срок своим (раньше перенесённый
    срок возвращался к дате с сайта при следующей синхронизации), а дату с
    сайта хранит в source_deadline. Возвращает {"title", "old", "new"}."""
    with file_lock(TASKS_FILE):
        tasks = read_json(TASKS_FILE) or []
        for t in tasks:
            if str(t.get("id")) == str(task_id):
                old = t.get("deadline")
                if t.get("source") in ("lms", "netology") and "source_deadline" not in t:
                    t["source_deadline"] = old
                t["deadline"] = deadline_iso
                t["deadline_overridden"] = True
                write_json(TASKS_FILE, tasks)
                return {"title": t.get("title", ""), "old": old, "new": deadline_iso}
    return None


def get_pending_tasks() -> list:
    return [t for t in get_tasks() if not t.get("done") and t.get("source") != "reminder_only"]

def get_seen_messages() -> list:
    return read_json(SEEN_MESSAGES_FILE) or []

def add_seen_message(message_id: str) -> None:
    with file_lock(SEEN_MESSAGES_FILE):
        seen = read_json(SEEN_MESSAGES_FILE) or []
        if message_id not in seen:
            seen.append(message_id)
            write_json(SEEN_MESSAGES_FILE, seen)

def is_seen(message_id: str) -> bool:
    return message_id in get_seen_messages()

def get_tokens() -> dict:
    return read_json(TOKENS_FILE) or {}

def save_token(key: str, value: str) -> None:
    with file_lock(TOKENS_FILE):
        tokens = read_json(TOKENS_FILE) or {}
        tokens[key] = value
        write_json(TOKENS_FILE, tokens)

def get_token(key: str) -> str | None:
    return get_tokens().get(key)


def mark_lms_tasks_done(completed_ids: set, parser_tasks: list = None) -> int:
    """
    Помечает LMS задачи выполненными.
    1. По прямому совпадению ID
    2. По названию+курсу — решает проблему дублей с разными ID
    3. По отсутствию в новом списке парсера — если задача исчезла из списка активных
    """
    import datetime
    from config import UFA_TZ
    count = 0
    now = datetime.datetime.now(tz=UFA_TZ).isoformat()
    completed_ids_str = {str(i) for i in completed_ids}

    with file_lock(TASKS_FILE):
        tasks = read_json(TASKS_FILE) or []

        for task in tasks:
            if task.get("done"):
                continue
            if task.get("source") != "lms":
                continue

            task_id = str(task.get("id", ""))

            # Способ 1: прямой ID
            if task_id in completed_ids_str:
                task["done"] = True
                task["done_at"] = now
                count += 1
                print(f"LMS done by ID: {task.get('title','')[:40]}")
                continue

            # Способ 2: отключён — исчезновение из парсера не означает выполнение.
            # Парсер может не вернуть задачу из-за сетевой ошибки, таймаута или нестандартного HTML.
            # Задача помечается выполненной только по прямому ID оценки (Способ 1).

        if count:
            write_json(TASKS_FILE, tasks)
    return count


def mark_netology_tasks_done(completed_ids: set) -> int:
    """Аналог mark_lms_tasks_done для Нетологии. Раньше такого не было вообще —
    parsers.netology.fetch_netology_deadlines() просто переставал возвращать
    задание, как только Нетология считала его сданным (passed=True), а
    старая запись в tasks.json так и оставалась done=False навсегда: ничто
    её не закрывало. Теперь парсер отдаёт id таких заданий сюда явно."""
    import datetime
    from config import UFA_TZ
    count = 0
    now = datetime.datetime.now(tz=UFA_TZ).isoformat()
    completed_ids_str = {str(i) for i in completed_ids}
    if not completed_ids_str:
        return 0

    with file_lock(TASKS_FILE):
        tasks = read_json(TASKS_FILE) or []
        for task in tasks:
            if task.get("done"):
                continue
            if task.get("source") != "netology":
                continue
            if str(task.get("id", "")) in completed_ids_str:
                task["done"] = True
                task["done_at"] = now
                count += 1
                print(f"Netology done by ID: {task.get('title','')[:40]}")
        if count:
            write_json(TASKS_FILE, tasks)
    return count
