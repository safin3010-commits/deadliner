#!/usr/bin/env python3
"""
Мост между окном наставника (scripts/native_widget, Swift) и данными бота.

  mentor_widget_action.py render
      пересобрать data/mentor_feed.html (окно зовёт при изменении
      tasks.json/reminders.json/расписания, при смене суток, после сна);
  mentor_widget_action.py refresh
      кнопка «Обновить» в окне: свежие задачи LMS/Нетологии, расписание,
      ссылки на вебинары и рендер — без Claude, 0 токенов;
  mentor_widget_action.py complete --id ID --title "точное название"
      закрыть задачу галочкой: task_actions.complete_task (та же логика, что
      у бота), затем Telegram-уведомление с кнопкой "↩️ Вернуть" и рендер.

Ответ — одна строка JSON в stdout: {"ok": bool, "error": "...", ...}.
Окно само проверяет формат id и одноразовый nonce страницы; здесь — вторая
линия защиты: id по белому списку символов, название должно совпасть с
текущим в tasks.json (устаревшая страница не закроет чужую запись).
"""
import argparse
import datetime
import html
import json
import os
import re
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)
sys.path.insert(0, os.path.join(PROJECT_DIR, "scripts"))
os.chdir(PROJECT_DIR)

LOG_FILE = os.path.join(PROJECT_DIR, "data", "mentor_widget_actions.log")
REFRESH_LOCK = os.path.join(PROJECT_DIR, "data", "mentor_refresh.lock")
ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def log(msg: str):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    except Exception:
        pass


def notify_telegram(title: str, action_id: str):
    """Тихое (без звука) уведомление — след в истории и откат с телефона."""
    try:
        import requests
        from config import TELEGRAM_TOKEN, MY_TELEGRAM_ID
        if not TELEGRAM_TOKEN or not MY_TELEGRAM_ID:
            return
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": MY_TELEGRAM_ID,
                "text": f"☑️ Закрыто на рабочем столе: <b>{html.escape(title)}</b>",
                "parse_mode": "HTML",
                "disable_notification": True,
                "reply_markup": {"inline_keyboard": [[
                    {"text": "↩️ Вернуть", "callback_data": f"wundo:{action_id}"},
                ]]},
            },
            timeout=15,
        )
    except Exception as e:
        log(f"telegram: не отправлено: {e!r}")


def cmd_render() -> dict:
    from mentor_dashboard import render_feed
    changed = render_feed()
    return {"ok": True, "changed": changed}


def cmd_refresh() -> dict:
    import asyncio
    import contextlib
    import fcntl
    lock = open(REFRESH_LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return {"ok": False, "error": "уже обновляется"}

    problems = []
    # Парсеры и планировщик печатают в stdout — а stdout у нас ответ окну.
    with open(LOG_FILE, "a", encoding="utf-8") as logf, contextlib.redirect_stdout(logf):
        try:
            from scheduler import sync_all_tasks, refresh_schedule_cache_silent
            asyncio.run(asyncio.wait_for(sync_all_tasks(), timeout=150))
        except Exception as e:
            problems.append("задачи")
            log(f"refresh: sync_all_tasks: {e!r}")
        try:
            asyncio.run(asyncio.wait_for(refresh_schedule_cache_silent(), timeout=120))
        except Exception as e:
            problems.append("расписание")
            log(f"refresh: расписание: {e!r}")
        try:
            from mentor_hourly_watch import refresh_yac_schedule
            refresh_yac_schedule()
        except Exception as e:
            problems.append("ссылки")
            log(f"refresh: yac: {e!r}")

        cmd_render()

    msg = "Обновлено"
    if problems:
        msg += " · не удалось: " + ", ".join(problems)
    log(f"refresh: {msg}")
    return {"ok": True, "message": msg}


def cmd_complete(task_id: str, title: str) -> tuple[dict, callable]:
    """(ответ окну, медленный хвост). Хвост — Telegram и «Напоминания» macOS —
    выполняется ПОСЛЕ ответа, чтобы галочка не висела по 10+ секунд."""
    if not ID_RE.match(task_id or ""):
        log(f"complete: отклонён некорректный id {task_id!r}")
        return {"ok": False, "error": "некорректный id"}, None
    from task_actions import complete_task, delete_macos_reminders
    result = complete_task(task_id, origin="desktop_widget", expected_title=title, delete_macos=False)
    if not result["ok"]:
        log(f"complete: id={task_id} отказ: {result['error']}")
        return result, None
    log(f"complete: id={task_id} «{result['title']}» action={result['action_id']}")
    try:
        cmd_render()
    except Exception as e:
        log(f"render после complete упал: {e!r}")

    def tail():
        notify_telegram(result["title"], result["action_id"])
        delete_macos_reminders(result["title"])
    return result, tail


def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["render", "complete", "refresh"])
    p.add_argument("--id")
    p.add_argument("--title")
    a = p.parse_args()
    tail = None
    try:
        if a.command == "render":
            out = cmd_render()
        elif a.command == "refresh":
            out = cmd_refresh()
        else:
            out, tail = cmd_complete(a.id or "", a.title if a.title is not None else "")
    except Exception as e:
        log(f"{a.command}: исключение {e!r}")
        out = {"ok": False, "error": f"внутренняя ошибка: {e.__class__.__name__}"}
    print(json.dumps(out, ensure_ascii=False), flush=True)
    if tail:
        # Окно читает stdout до EOF — закрываем его, ответ уже у окна.
        try:
            os.close(1)
        except OSError:
            pass
        try:
            tail()
        except Exception as e:
            log(f"complete: хвост упал: {e!r}")


if __name__ == "__main__":
    main()
