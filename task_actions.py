"""
Единая точка "закрыть задачу / снять напоминание" для ВСЕХ мест, откуда это
можно сделать: бот (свободный текст, кнопка "✅ Сделано" в напоминании),
окно наставника на рабочем столе (галочка). Раньше каждая точка закрывала
по-своему: кнопка в боте удаляла только одно напоминание, smart_intent —
только напоминания reminder_only-задач (у обычной LMS-задачи с
напоминанием оно продолжало звонить после "сделано"), и ни одна точка не
трогала копию в приложении «Напоминания» macOS, которую создаёт
scheduler.check_user_reminders при каждом срабатывании.

complete_task() закрывает задачу везде, где она хранится:
  1. data/tasks.json — обычная задача → done=True (+manually_done для
     стрика), служебная reminder_only → удаляется (как и раньше в боте);
  2. data/reminders.json — ВСЕ напоминания этой задачи;
  3. приложение «Напоминания» macOS — одноимённые пункты (best-effort);
и сохраняет полную копию затронутых записей в data/task_actions.json —
undo_action() по ней возвращает всё как было (кнопка "↩️ Вернуть" в
Telegram-уведомлении о закрытии с рабочего стола).

Локи: tasks.json → reminders.json, всегда в этом порядке (file_lock на flock
не реентерабелен — внутри не вызываем mark_task_done/delete_reminder, они
берут те же локи сами).
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import uuid

from config import TASKS_FILE, UFA_TZ
from data_lock import atomic_write_json, file_lock

REMINDERS_FILE = "data/reminders.json"
ACTIONS_FILE = "data/task_actions.json"
ACTIONS_KEEP = 60
UNDO_WINDOW_HOURS = 48


def _read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=UFA_TZ)


def delete_macos_reminders(title: str):
    """scheduler.check_user_reminders при каждом срабатывании создаёт пункт с
    именем title[:50] в списке по умолчанию «Напоминаний» — после закрытия он
    висел бы там до следующего (уже несуществующего) срабатывания."""
    name = (title or "")[:50].replace("\\", "\\\\").replace('"', '\\"')
    if not name:
        return
    script = (
        'tell application "Reminders" to delete '
        f'(every reminder whose name is "{name}" and completed is false)'
    )
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except Exception:
        pass


def _journal_write(entry: dict):
    """Журнал действий (data/task_actions.json). Вызывается ВНУТРИ локов
    tasks → reminders: порядок локов один везде (tasks → reminders → actions),
    иначе два процесса могли бы ждать друг друга вечно."""
    with file_lock(ACTIONS_FILE):
        actions = _read(ACTIONS_FILE, [])
        actions = [a for a in actions if a.get("id") != entry["id"]] + [entry]
        atomic_write_json(ACTIONS_FILE, actions[-ACTIONS_KEEP:])


def complete_task(task_id, origin: str, expected_title: str | None = None,
                  delete_macos: bool = True) -> dict:
    """Закрыть задачу/напоминание по id. expected_title — защита от закрытия
    не той записи (окно на столе передаёт название, которое видел
    пользователь; если запись с этим id уже другая — отказываемся).

    Журнал ДО записи (ревью Codex 2026-10-02): копия задачи и напоминаний
    пишется в task_actions.json со статусом pending ПЕРЕД изменением данных,
    затем committed. Если запись tasks.json упадёт после удаления
    напоминаний — копия уже есть, undo_action её вернёт.

    Возвращает {"ok": bool, "error": str, "title": str, "action_id": str}."""
    task_id = str(task_id)
    action_id = uuid.uuid4().hex[:12]
    with file_lock(TASKS_FILE):
        tasks = _read(TASKS_FILE, [])
        task = next((t for t in tasks if str(t.get("id")) == task_id), None)
        if not task:
            return {"ok": False, "error": "задача не найдена (уже закрыта или удалена)"}
        if task.get("done"):
            return {"ok": False, "error": "задача уже отмечена выполненной"}
        if expected_title is not None and (task.get("title") or "").strip() != expected_title.strip():
            return {"ok": False, "error": "название не совпадает — окно устарело, обнови его"}

        backup_task = dict(task)
        title = task.get("title") or ""
        removed_task = task.get("source") == "reminder_only"
        if removed_task:
            tasks = [t for t in tasks if str(t.get("id")) != task_id]
        else:
            task["done"] = True
            task["done_at"] = _now().isoformat()
            task["manually_done"] = True

        with file_lock(REMINDERS_FILE):
            reminders = _read(REMINDERS_FILE, [])
            removed_reminders = [r for r in reminders if str(r.get("task_id")) == task_id]
            entry = {
                "id": action_id, "type": "complete", "origin": origin, "at": _now().isoformat(),
                "task": backup_task, "task_removed": removed_task,
                "reminders": removed_reminders, "undone": False, "status": "pending",
            }
            _journal_write(entry)
            if removed_reminders:
                atomic_write_json(
                    REMINDERS_FILE,
                    [r for r in reminders if str(r.get("task_id")) != task_id],
                )
            atomic_write_json(TASKS_FILE, tasks)
            entry["status"] = "committed"
            _journal_write(entry)

    if delete_macos:
        delete_macos_reminders(title)
    return {"ok": True, "error": "", "title": title, "action_id": action_id}


def undo_action(action_id: str) -> dict:
    """Вернуть задачу и её напоминания ровно в состояние до complete_task.
    Работает и для pending-записи (операция оборвалась посередине)."""
    snapshot = next((a for a in _read(ACTIONS_FILE, []) if a.get("id") == action_id), None)
    if not snapshot:
        return {"ok": False, "error": "действие не найдено (слишком старое?)"}
    try:
        age = _now() - datetime.datetime.fromisoformat(snapshot["at"])
        if age > datetime.timedelta(hours=UNDO_WINDOW_HOURS):
            return {"ok": False, "error": f"прошло больше {UNDO_WINDOW_HOURS} ч — верни вручную"}
    except Exception:
        pass

    backup = snapshot["task"]
    task_id = str(backup.get("id"))
    # Порядок локов тот же, что в complete_task: tasks → reminders → actions.
    with file_lock(TASKS_FILE):
        with file_lock(REMINDERS_FILE):
            with file_lock(ACTIONS_FILE):
                actions = _read(ACTIONS_FILE, [])
                action = next((a for a in actions if a.get("id") == action_id), None)
                if not action or action.get("undone"):
                    return {"ok": False, "error": "уже отменено"}

                tasks = _read(TASKS_FILE, [])
                current = next((t for t in tasks if str(t.get("id")) == task_id), None)
                if action.get("task_removed"):
                    if current is None:
                        tasks.append(backup)
                elif current is not None:
                    current["done"] = False
                    current.pop("done_at", None)
                    current.pop("manually_done", None)
                else:
                    tasks.append(backup)

                if action.get("reminders"):
                    reminders = _read(REMINDERS_FILE, [])
                    have = {r.get("id") for r in reminders}
                    now = _now()
                    for r in action["reminders"]:
                        if r.get("id") in have:
                            continue
                        r = dict(r)
                        # Пока задача была закрыта, срабатывания могли пройти —
                        # не выстреливаем пачкой сразу после отката.
                        try:
                            if datetime.datetime.fromisoformat(r["next_at"]) < now:
                                r["next_at"] = (now + datetime.timedelta(minutes=5)).isoformat()
                        except Exception:
                            pass
                        r.pop("last_message_id", None)
                        reminders.append(r)
                    atomic_write_json(REMINDERS_FILE, reminders)
                atomic_write_json(TASKS_FILE, tasks)

                action["undone"] = True
                action["undone_at"] = _now().isoformat()
                atomic_write_json(ACTIONS_FILE, actions)
    return {"ok": True, "error": "", "title": backup.get("title", "")}
