import json
import os
from typing import Any
from config import TASKS_FILE, SEEN_MESSAGES_FILE, TOKENS_FILE, DATA_DIR

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
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)

def get_tasks() -> list:
    return read_json(TASKS_FILE) or []

def save_tasks(tasks: list) -> None:
    write_json(TASKS_FILE, tasks)

def add_task(title: str, deadline: str | None, source: str) -> dict:
    tasks = get_tasks()
    # Берём максимальный числовой ID чтобы избежать дублей
    max_id = 0
    for t in tasks:
        try:
            tid = int(t["id"])
            if tid > max_id:
                max_id = tid
        except (ValueError, TypeError):
            pass
    task = {
        "id": max_id + 1,
        "title": title,
        "deadline": deadline,
        "source": source,
        "done": False,
    }
    tasks.append(task)
    save_tasks(tasks)
    return task

def mark_task_done(task_id) -> bool:
    """Отмечаем задачу выполненной. task_id может быть int или str."""
    import datetime
    from config import UFA_TZ
    tasks = get_tasks()
    for task in tasks:
        if str(task["id"]) == str(task_id):
            task["done"] = True
            task["done_at"] = datetime.datetime.now(tz=UFA_TZ).isoformat()
            save_tasks(tasks)
            return True
    return False

def get_pending_tasks() -> list:
    return [t for t in get_tasks() if not t.get("done")]

def get_seen_messages() -> list:
    return read_json(SEEN_MESSAGES_FILE) or []

def add_seen_message(message_id: str) -> None:
    seen = get_seen_messages()
    if message_id not in seen:
        seen.append(message_id)
        write_json(SEEN_MESSAGES_FILE, seen)

def is_seen(message_id: str) -> bool:
    return message_id in get_seen_messages()

def get_tokens() -> dict:
    return read_json(TOKENS_FILE) or {}

def save_token(key: str, value: str) -> None:
    tokens = get_tokens()
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
    tasks = get_tasks()
    count = 0
    now = datetime.datetime.now(tz=UFA_TZ).isoformat()
    completed_ids_str = {str(i) for i in completed_ids}

    # Строим индекс: title.lower+course.lower -> True для выполненных
    # Парсер НЕ включает выполненные в новый список — значит если задача есть в storage
    # но отсутствует в parser_tasks — она выполнена
    parser_ids = {str(t.get("id","")) for t in (parser_tasks or [])}
    parser_name_course = {
        (t.get("title","").strip().lower(), t.get("course_name","").strip()[:30].lower())
        for t in (parser_tasks or [])
    }

    for task in tasks:
        if task.get("done"):
            continue
        if task.get("source") != "lms":
            continue

        task_id = str(task.get("id", ""))
        title_key = task.get("title","").strip().lower()
        course_key = task.get("course_name","").strip()[:30].lower()
        name_pair = (title_key, course_key)

        # Способ 1: прямой ID
        if task_id in completed_ids_str:
            task["done"] = True
            task["done_at"] = now
            count += 1
            print(f"LMS done by ID: {task.get('title','')[:40]}")
            continue

        # Способ 2: совпадение названия+курс с выполненным (разные ID одного задания)
        # Если задача НЕ в новом списке парсера — парсер её отфильтровал как выполненную
        if parser_tasks is not None and name_pair not in parser_name_course:
            task["done"] = True
            task["done_at"] = now
            count += 1
            print(f"LMS done by absence: {task.get('title','')[:40]}")
            continue

    if count:
        save_tasks(tasks)
    return count
