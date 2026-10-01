#!/usr/bin/env python3
"""
Ночной дневник — пункт 4 плана улучшений. Раз в сутки дешёвая модель
сжимает прошедший день в 5–8 строк: что пришло (оценки, задания, важные
письма), что ты закрыл, что обещал, о чём говорили с ботом, что писал
наставник. Дневник подмешивается в сводки (3 дня — чек-ины и бот, 7 дней —
недельный итог): долгая память без пересылки всей истории.

Запускается из почасового сторожа: если за вчера записи нет — пишем.
Модель получает только факты дня (собраны кодом) и не выдумывает выводов
о человеке — это дневник событий, а не оценка личности.
"""
import datetime
import json
import os
import re
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)
sys.path.insert(0, os.path.join(PROJECT_DIR, "scripts"))
os.chdir(PROJECT_DIR)

from config import UFA_TZ


def day_facts(day: datetime.date) -> str:
    from agent_db import connect
    start = datetime.datetime.combine(day, datetime.time(0, 0), tzinfo=UFA_TZ).isoformat(timespec="seconds")
    end = datetime.datetime.combine(day + datetime.timedelta(days=1), datetime.time(0, 0),
                                    tzinfo=UFA_TZ).isoformat(timespec="seconds")
    conn = connect()
    try:
        events = conn.execute(
            "SELECT e.kind, e.source, e.course, e.title, e.body, e.sender, x.result_json FROM raw_events e "
            "LEFT JOIN extractions x ON x.event_id = e.id "
            "WHERE COALESCE(e.occurred_at, e.observed_at) >= ? AND COALESCE(e.occurred_at, e.observed_at) < ? "
            "ORDER BY e.id", (start, end)).fetchall()
        dialog = conn.execute("SELECT role, text FROM dialog_messages WHERE at >= ? AND at < ? ORDER BY id",
                              (start, end)).fetchall()
        commits = conn.execute("SELECT action, status, due_at FROM commitments WHERE "
                               "(created_at >= ? AND created_at < ?) OR (closed_at >= ? AND closed_at < ?)",
                               (start, end, start, end)).fetchall()
    finally:
        conn.close()

    lines = []
    for e in events:
        res = json.loads(e["result_json"]) if e["result_json"] else {}
        if e["kind"] == "grade":
            if re.fullmatch(r"оценка 0(\.0+)?", (e["body"] or "").strip()):
                continue  # 0 в Modeus — «не выставлено», не двойка
            lines.append(f"оценка: {e['course'] or ''} {e['title'] or ''} — {e['body']}")
        elif e["kind"] == "task_new":
            lines.append(f"новое задание: {e['title']} ({e['course'] or ''})")
        elif e["kind"] == "user_said":
            continue  # есть в диалоге
        elif res.get("relevant"):
            lines.append(f"{e['source']} от {(e['sender'] or '')[:40]}: {res.get('summary') or (e['title'] or '')[:120]}"
                         + (" [ждёт ответа]" if res.get("needs_reply") else ""))

    from storage import get_tasks
    for t in get_tasks():
        at = t.get("done_at") or ""
        if t.get("done") and at[:10] == day.isoformat():
            from mentor_dashboard import short_course
            course = short_course(t.get("course_name"))
            lines.append(f"закрыто{' им самим' if t.get('manually_done') else ''}: {t.get('title', '')[:90]}"
                         + (f" ({course})" if course else ""))
    for c in commits:
        lines.append(f"обещание «{c['action']}»: {c['status']}")
    for d in dialog:
        lines.append(f"{'он боту' if d['role'] == 'user' else 'бот'}: {d['text'][:200]}")
    try:
        with open(os.path.join(PROJECT_DIR, "data", "mentor_sent.json"), encoding="utf-8") as f:
            for s in json.load(f):
                if s.get("at", "")[:10] == day.isoformat():
                    lines.append(f"наставник ({s.get('slot')}): {s.get('text', '')[:250]}")
    except Exception:
        pass
    return "\n".join(f"- {l}" for l in lines)


def write_diary(day: datetime.date) -> str:
    from agent_db import save_diary
    from claude_session import run_claude_oneshot
    facts = day_facts(day)
    if not facts.strip():
        text = "Событий не зафиксировано."
    else:
        prompt = (
            f"Факты учебного дня студента за {day.strftime('%d.%m.%Y')} (собраны кодом):\n{facts[:12000]}\n\n"
            "Сожми в дневниковую запись из 3–8 коротких строк: что пришло важного (оценки, задания, письма "
            "с просьбами/сроками), что он закрыл, какие обещания дал или выполнил, о чём спрашивал бота. "
            "Только факты из списка, без оценок личности и без советов. Без вступлений."
        )
        text, err = run_claude_oneshot(prompt, "Ты ведёшь краткий фактический дневник учёбы. Пишешь по-русски.",
                                       120, "claude-sonnet-5", "diary")
        if err:
            raise RuntimeError(err)
    text = re.sub(r"\*\*|__", "", text)
    text = "\n".join(l for l in text.strip().splitlines()
                     if not re.fullmatch(r"\s*\d{1,2}\.\d{1,2}\.\d{2,4}\s*:?\s*", l)).strip()
    save_diary(day.isoformat(), text)
    return text


def ensure_yesterday() -> str | None:
    """Вызывается сторожем: дописать вчерашний день, если записи ещё нет."""
    from agent_db import has_diary
    now = datetime.datetime.now(tz=UFA_TZ)
    day = now.date() - datetime.timedelta(days=1)
    if has_diary(day.isoformat()):
        return None
    return write_diary(day)


if __name__ == "__main__":
    d = datetime.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else \
        datetime.datetime.now(tz=UFA_TZ).date() - datetime.timedelta(days=1)
    print(write_diary(d))
