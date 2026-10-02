"""
Context pack для чек-инов наставника — шаг 1 плана design/mentor_agent_architecture.md.

Раньше чек-ин шёл через `claude -p --resume <сессия дня>` с инструментами
Read/Glob: модель сама бегала по сырым JSON (до 27 обращений) и каждый раз
заново получала весь разговор дня — 0,3–1,3 млн токенов за утренний вызов.
Теперь код собирает компактную сводку (цель ≤ ~6 тыс. токенов), а модель
получает только её и пишет текст, без инструментов и без сессии.

Правило: всё, что здесь, — факты из файлов или детерминированные проекции
(списки из mentor_dashboard). Выводов модели сюда не кладём, кроме памяти
«что уже писал» — она явно подписана как прошлые сообщения, а не факты.
"""
from __future__ import annotations

import datetime
import json
import os
import re

from config import UFA_TZ

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_DIR, "data")
SENT_FILE = os.path.join(DATA_DIR, "mentor_sent.json")
SENT_KEEP = 30

_DAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def _load(name, default):
    try:
        with open(os.path.join(DATA_DIR, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _read_text(name) -> str:
    try:
        with open(os.path.join(DATA_DIR, name), encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _clip(text: str, n: int) -> str:
    text = re.sub(r"[​-‏⁠­‌]+", "", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _parse_any_dt(value) -> datetime.datetime | None:
    if not value:
        return None
    for fmt in (None, "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            dt = datetime.datetime.fromisoformat(value) if fmt is None else datetime.datetime.strptime(value, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=UFA_TZ)
        except (ValueError, TypeError):
            continue
    return None


# ─── память о том, что уже написано ─────────────────────────────────

def record_sent(slot: str, text: str, feedback_id: str = ""):
    """Что наставник реально отправил — вместо --resume дневной сессии
    это и даёт связность: следующий чек-ин видит, что уже говорилось."""
    from data_lock import atomic_write_json, file_lock
    with file_lock(SENT_FILE):
        sent = _load("mentor_sent.json", [])
        sent.append({"at": datetime.datetime.now(tz=UFA_TZ).isoformat(), "slot": slot,
                     "text": text, "feedback_id": feedback_id})
        atomic_write_json(SENT_FILE, sent[-SENT_KEEP:])


def _sent_block(now: datetime.datetime) -> str:
    sent = _load("mentor_sent.json", [])
    since = now - datetime.timedelta(hours=30)
    lines = []
    for s in sent:
        at = _parse_any_dt(s.get("at"))
        if at and at >= since:
            plain = re.sub(r"</?[bi]>", "", s.get("text", ""))
            lines.append(f"- {at.strftime('%d.%m %H:%M')} ({s.get('slot')}): {_clip(plain, 500)}")
    return "\n".join(lines[-4:])


# ─── блоки данных ────────────────────────────────────────────────────

def _lists(model) -> dict:
    from mentor_dashboard import _sched_text, _item_text
    rem = []
    for r in model["reminders"]:
        ev = f"событие {r['event_date'].strftime('%d.%m')}; " if r["event_date"] else ""
        nxt = f"сработает {r['next_at'].strftime('%d.%m %H:%M')}" if r["next_at"] else "отработало, ждёт закрытия"
        rem.append(f"- {r['title']} ({ev}{nxt})")
    overdue = model["overdue"]
    return {
        "today": "\n".join(_sched_text(model["today"])),
        "tomorrow": "\n".join(_sched_text(model["tomorrow"])),
        "deadlines": "\n".join(_item_text(i) for i in model["deadlines"]) or "(нет)",
        "overdue": ("\n".join(_item_text(i) for i in overdue[:12])
                    + (f"\n… и ещё {len(overdue) - 12}" if len(overdue) > 12 else "")) or "(нет)",
        "reminders": "\n".join(rem) or "(нет)",
    }


def _done_today(now) -> str:
    out = []
    for t in _load("tasks.json", []):
        at = _parse_any_dt(t.get("done_at"))
        if t.get("done") and at and at.date() == now.date():
            how = "вручную" if t.get("manually_done") else "засчитано системой"
            out.append(f"- {t.get('title', '')[:90]} ({how})")
    return "\n".join(out) or "(сегодня ничего не закрыто)"


def _activity(now) -> str:
    stats = _load("daily_stats.json", {})
    days = sorted(stats)[-7:]
    rows = [f"{d[8:10]}.{d[5:7]}: закрыто {stats[d].get('done', 0)}, в работе {stats[d].get('pending', '?')}, пар {stats[d].get('lessons', '?')}"
            for d in days]
    try:
        from streak import compute_streak
        streak = compute_streak()
    except Exception:
        streak = _load("streak.json", {})
    last = streak.get("last_active", "")
    note = f"стрик {streak.get('streak', 0)} (макс {streak.get('max_streak', 0)}), последняя активность {last}"
    return "\n".join(rows + [note])


def _weather() -> str:
    w = _read_text("weather_latest.txt")
    w = re.sub(r"[*_─]+", "", w)
    return "\n".join(l.strip() for l in w.splitlines() if l.strip())[:700]


def _study_analysis(now) -> str:
    text = _read_text("study_analysis_latest.txt")
    if not text:
        return ""
    m = re.search(r"Обновлено:\s*(\S+)", text)
    at = _parse_any_dt(m.group(1)) if m else None
    stale = ""
    if at and (now - at).days >= 3:
        stale = f"(ВНИМАНИЕ: данные устарели — от {at.strftime('%d.%m')}, не выдавай их за свежие)\n"
    return stale + text[:2500]


def _comms(now, hours=26) -> str:
    since = now - datetime.timedelta(hours=hours)
    out = []
    for m in _load("mail_recent.json", []):
        at = _parse_any_dt(m.get("date"))
        if at and at >= since:
            out.append(f"- письмо {at.strftime('%d.%m %H:%M')} от {_clip(m.get('sender', ''), 40)}: "
                       f"«{_clip(m.get('subject', ''), 120)}» — {_clip(m.get('body', ''), 160)}")
    msgs = [m for m in _load("messenger_recent.json", []) if (_parse_any_dt(m.get("at")) or since) >= since]
    for m in msgs[-10:]:
        out.append(f"- мессенджер, {_clip(m.get('sender', ''), 40)}: {_clip(m.get('text', ''), 150)}")
    for n in _load("netology_notif_recent.json", [])[-5:]:
        at = _parse_any_dt(n.get("at") or n.get("date"))
        if at is None or at >= since:
            out.append(f"- уведомление Нетологии: {_clip(n.get('title', ''), 140)}")
    return "\n".join(out[:20])


def _vk_schedule() -> str:
    entries = _load("vk_schedule_updates.json", [])
    texts = [_clip(e.get("text", ""), 600) for e in entries[-6:]]
    overrides = [o.get("note", "") for o in _load("schedule_overrides.json", [])]
    parts = []
    if texts:
        parts.append("Сырые объявления из беседы ВК:\n" + "\n---\n".join(texts))
    if overrides:
        parts.append("Уже известные переносы/отмены:\n" + "\n".join(f"- {o}" for o in overrides))
    return "\n\n".join(parts)


def _today_events(now) -> str:
    """Новые оценки и новые задания за сутки — раньше наставник мог
    наткнуться на них сам, читая файлы; в сводке их не было."""
    try:
        from agent_db import connect
    except Exception:
        return ""
    since = (now - datetime.timedelta(hours=26)).isoformat(timespec="seconds")
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT kind, course, title, body, effective_at FROM raw_events "
            "WHERE kind IN ('grade','task_new') AND COALESCE(occurred_at, observed_at) >= ? "
            "ORDER BY id", (since,)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        if r["kind"] == "grade":
            if re.fullmatch(r"оценка 0(\.0+)?", (r["body"] or "").strip()):
                continue  # 0 в Modeus — «не выставлено», не двойка
            out.append(f"- оценка: {r['title'] or r['course']} — {r['body']}")
        else:
            dl = f", срок {r['effective_at'][8:10]}.{r['effective_at'][5:7]}" if r["effective_at"] else ""
            out.append(f"- новое задание: {r['title']} ({r['course'] or '—'}{dl})")
    return "\n".join(out[:15])


def _needs_reply(now) -> str:
    """Письма/сообщения, на которые, по разбору (scripts/agent_extract.py),
    нужно ответить — за последние 3 дня."""
    try:
        from agent_db import connect
    except Exception:
        return ""
    since = (now - datetime.timedelta(days=3)).isoformat(timespec="seconds")
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT e.source, e.sender, e.title, e.body, x.result_json FROM extractions x "
            "JOIN raw_events e ON e.id = x.event_id WHERE e.observed_at >= ?", (since,)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        res = json.loads(r["result_json"] or "{}")
        if res.get("needs_reply"):
            out.append(f"- {r['source']}, {_clip(r['sender'] or '', 40)}: {_clip(res.get('summary') or r['title'] or r['body'] or '', 160)}")
    return "\n".join(out[:8])


def _diary(days: int) -> str:
    try:
        from agent_db import get_diary
        return "\n".join(f"{d['date'][8:10]}.{d['date'][5:7]}: {d['text']}" for d in get_diary(days))
    except Exception:
        return ""


def _memory() -> str:
    parts = []
    try:
        from agent_db import confirmed_profile
        prof = confirmed_profile()
        if prof:
            parts.append("Подтверждено им самим:\n" + "\n".join(f"- {p}" for p in prof))
    except Exception:
        pass
    try:
        from scheduler import get_ai_memory_recap
        parts.append(get_ai_memory_recap()[:2500])
    except Exception:
        pass
    return "\n\n".join(p for p in parts if p)


def _knowledge() -> str:
    try:
        from mentor_checkin import _deterministic_knowledge_block
        return _deterministic_knowledge_block()[:2500]
    except Exception:
        return ""


# ─── сборка ─────────────────────────────────────────────────────────

# Какие блоки нужны какому слоту — модель получает только релевантное.
SLOT_BLOCKS = {
    "morning":        ["today", "new_events", "deadlines", "overdue", "reminders", "commitments", "needs_reply", "diary", "weather", "comms", "knowledge"],
    "midmorning":     ["today", "deadlines", "reminders", "done_today", "activity"],
    "schedule_focus": ["today", "tomorrow", "vk", "knowledge"],
    "motivation":     ["deadlines", "overdue", "done_today", "activity"],
    "evening":        ["done_today", "new_events", "tomorrow", "deadlines", "overdue", "commitments", "needs_reply", "activity", "study", "comms"],
    "winddown":       ["tomorrow", "new_events", "deadlines", "overdue", "reminders", "commitments", "needs_reply", "done_today"],
    "initiative":     ["today", "tomorrow", "new_events", "deadlines", "overdue", "commitments", "needs_reply"],
    "weekly":         ["diary_week", "activity", "commitments_week", "grades_week", "done_today", "overdue", "deadlines", "commitments", "study"],
}

TITLES = {
    "today": "РАСПИСАНИЕ СЕГОДНЯ (код, точное)",
    "tomorrow": "РАСПИСАНИЕ ЗАВТРА (код, точное)",
    "deadlines": "ДЕДЛАЙНЫ: СЕГОДНЯ + 3 ДНЯ (код, точное)",
    "overdue": "ПРОСРОЧЕНО (код; самые старые первыми)",
    "reminders": "ЛИЧНЫЕ НАПОМИНАНИЯ (дата события ≠ время срабатывания)",
    "done_today": "ЗАКРЫТО СЕГОДНЯ",
    "activity": "АКТИВНОСТЬ ПО ДНЯМ",
    "weather": "ПОГОДА",
    "study": "БАЛЛЫ И ПОСЕЩАЕМОСТЬ (Modeus)",
    "comms": "ПОЧТА / МЕССЕНДЖЕР / НЕТОЛОГИЯ ЗА СУТКИ (уже пришли форвардами — только напомнить о важном)",
    "vk": "ВК: ИЗМЕНЕНИЯ РАСПИСАНИЯ",
    "knowledge": "ОРГФАКТЫ С ВЕБИНАРОВ ПО СЕГОДНЯШНИМ ПРЕДМЕТАМ",
    "commitments": "ЕГО ОБЕЩАНИЯ (сам подтвердил; если срок прошёл — мягко спроси, как дела)",
    "new_events": "НОВОЕ ЗА СУТКИ: ОЦЕНКИ И ЗАДАНИЯ",
    "needs_reply": "ЖДУТ ЕГО ОТВЕТА (письма/сообщения преподавателей и т.п.)",
    "diary": "ДНЕВНИК ПОСЛЕДНИХ ДНЕЙ (сжатые итоги)",
    "diary_week": "ДНЕВНИК НЕДЕЛИ (сжатые итоги по дням)",
    "commitments_week": "ОБЕЩАНИЯ ЗА НЕДЕЛЮ: ВЫПОЛНЕНО / ПРОПУЩЕНО",
    "grades_week": "ОЦЕНКИ ЗА НЕДЕЛЮ",
}


def _commitments_week(now) -> str:
    try:
        from agent_db import connect
    except Exception:
        return ""
    since = (now - datetime.timedelta(days=7)).isoformat(timespec="seconds")
    conn = connect()
    try:
        rows = conn.execute("SELECT action, status, due_at FROM commitments WHERE created_at >= ? "
                            "AND status IN ('open','done','cancelled')", (since,)).fetchall()
    finally:
        conn.close()
    today = now.date().isoformat()
    out = []
    for r in rows:
        st = {"done": "выполнено", "cancelled": "снято"}.get(r["status"], "открыто")
        if r["status"] == "open" and r["due_at"] and r["due_at"][:10] < today:
            st = "СРОК ПРОШЁЛ, не выполнено"
        out.append(f"- {r['action']}: {st}")
    return "\n".join(out)


def _grades_week(now) -> str:
    try:
        from agent_db import connect
    except Exception:
        return ""
    since = (now - datetime.timedelta(days=7)).isoformat(timespec="seconds")
    conn = connect()
    try:
        rows = conn.execute("SELECT course, title, body FROM raw_events WHERE kind='grade' "
                            "AND COALESCE(occurred_at, observed_at) >= ? ORDER BY id", (since,)).fetchall()
    finally:
        conn.close()
    return "\n".join(f"- {r['course'] or ''} {r['title'] or ''}: {r['body']}" for r in rows
                     if not re.fullmatch(r"оценка 0(\.0+)?", (r["body"] or "").strip()))


BLOCK_MAX_CHARS = 3500     # один блок сводки
PACK_MAX_CHARS = 26000     # вся сводка (~10–12 тыс. токенов) — жёсткий потолок


def _cap(parts: list[str]) -> str:
    """Общий предел размера сводки (ревью Codex: профиль, дневник, задачи
    растут без ограничений). Блоки идут по важности — сверху; хвост, не
    влезающий в предел, отбрасываем с пометкой, а не молча."""
    out, used = [], 0
    for p in parts:
        if len(p) > BLOCK_MAX_CHARS:
            p = p[:BLOCK_MAX_CHARS] + "\n… (блок обрезан)"
        if used + len(p) > PACK_MAX_CHARS:
            out.append("=== (остальные блоки не поместились в лимит сводки) ===")
            break
        out.append(p)
        used += len(p) + 2
    return "\n\n".join(out)


def build_checkin_pack(slot: str, extra_blocks: list[str] | None = None) -> str:
    import sys
    sys.path.insert(0, os.path.join(PROJECT_DIR, "scripts"))
    from mentor_dashboard import build_model

    now = datetime.datetime.now(tz=UFA_TZ)
    model = build_model(now)
    lists = _lists(model)
    makers = {
        "today": lambda: lists["today"],
        "tomorrow": lambda: lists["tomorrow"],
        "deadlines": lambda: lists["deadlines"],
        "overdue": lambda: lists["overdue"],
        "reminders": lambda: lists["reminders"],
        "done_today": lambda: _done_today(now),
        "activity": lambda: _activity(now),
        "weather": _weather,
        "study": lambda: _study_analysis(now),
        "comms": lambda: _comms(now),
        "vk": _vk_schedule,
        "knowledge": _knowledge,
        "commitments": lambda: _commitments_block(),
        "new_events": lambda: _today_events(now),
        "needs_reply": lambda: _needs_reply(now),
        "diary": lambda: _diary(3),
        "diary_week": lambda: _diary(7),
        "commitments_week": lambda: _commitments_week(now),
        "grades_week": lambda: _grades_week(now),
    }
    keys = list(SLOT_BLOCKS.get(slot, ["today", "deadlines", "overdue"]))
    for k in extra_blocks or []:
        if k not in keys:
            keys.append(k)

    parts = [f"=== СЕЙЧАС ===\n{now.strftime('%d.%m.%Y')} ({_DAYS_RU[now.weekday()]}), {now.strftime('%H:%M')}"]
    mem = _memory()
    if mem:
        parts.append(f"=== ПАМЯТЬ О СТУДЕНТЕ (сводки прошлых дней) ===\n{mem}")
    sent = _sent_block(now)
    if sent:
        parts.append(f"=== ТВОИ ПОСЛЕДНИЕ СООБЩЕНИЯ ЕМУ (не повторяйся) ===\n{sent}")
    for k in keys:
        body = (makers[k]() or "").strip()
        if body:
            parts.append(f"=== {TITLES[k]} ===\n{body}")
    return _cap(parts)


# ─── контекст для разговора с ботом (bot/smart_intent.py) ───────────

def _pending_tasks_block() -> str:
    """Все активные задачи с id — модели нужно сопоставить «закрой лабу 3»
    с конкретной записью (type=complete)."""
    from mentor_dashboard import clean_title, short_course
    lines = []
    for t in _load("tasks.json", []):
        if t.get("done"):
            continue
        dl = (t.get("deadline") or "")[:10]
        dl = f" | срок {dl[8:10]}.{dl[5:7]}" if dl else ""
        kind = " | личное напоминание" if t.get("source") == "reminder_only" else ""
        course = short_course(t.get("course_name"))
        lines.append(f"{t.get('id')} | {clean_title(t.get('title', ''))[:80]}"
                     f"{' | ' + course if course else ''}{dl}{kind}")
    return "\n".join(lines)


def _week_schedule() -> str:
    from mentor_dashboard import build_schedule
    today = datetime.datetime.now(tz=UFA_TZ).date()
    out = []
    for i in range(7):
        d = today + datetime.timedelta(days=i)
        s = build_schedule(d)
        if s["lines"]:
            day = f"{d.strftime('%d.%m')} {_DAYS_RU[d.weekday()][:2]}"
            out.append(day + ": " + "; ".join(f"{l['time']} {l['label'][:60]}" for l in s["lines"]))
    return "\n".join(out) or "(пар на неделю нет)"


def _commitments_block() -> str:
    try:
        from agent_db import list_commitments
        rows = list_commitments("open", 15)
    except Exception:
        return ""
    now = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
    out = []
    for r in rows:
        late = " (СРОК ПРОШЁЛ)" if r.get("due_at") and r["due_at"][:10] < now else ""
        due = f" до {r['due_at'][8:10]}.{r['due_at'][5:7]}" if r.get("due_at") else ""
        out.append(f"- {r['action']}{due}{late} — «{r['quote'][:100]}»")
    return "\n".join(out)


def build_chat_pack(user_text: str) -> str:
    """Контекст для одного сообщения пользователя боту: задачи с id,
    расписание на неделю, дедлайны/просрочки, напоминания, обещания, недавний
    диалог и найденное поиском по журналу событий и оргфактам. Вместо
    --resume дневной сессии с Read/Grep по data/*.json."""
    import sys
    sys.path.insert(0, os.path.join(PROJECT_DIR, "scripts"))
    from mentor_dashboard import build_model
    now = datetime.datetime.now(tz=UFA_TZ)
    model = build_model(now)
    lists = _lists(model)
    parts = [f"=== СЕЙЧАС ===\n{now.strftime('%d.%m.%Y')} ({_DAYS_RU[now.weekday()]}), {now.strftime('%H:%M')}"]

    try:
        from agent_db import recent_dialog, search_events, search_knowledge
        dialog = recent_dialog(limit=30, hours=72)
        if dialog:
            parts.append("=== НЕДАВНИЙ ДИАЛОГ (для контекста продолжений) ===\n" + "\n".join(
                f"{'Он' if d['role'] == 'user' else 'Ты'} ({d['at'][11:16]}): {_clip(d['text'], 300)}" for d in dialog))
        found = search_knowledge(user_text, limit=6)
        if found:
            parts.append("=== ОРГФАКТЫ С ВЕБИНАРОВ (поиск по вопросу; подтверждённые) ===\n" + "\n".join(
                f"- [{f['course']}; {f['source'] or 'вебинар'}] {_clip(f['text'], 300)}" for f in found))
        events = search_events(user_text, limit=6)
        if events:
            parts.append("=== ИЗ ПЕРЕПИСКИ И УВЕДОМЛЕНИЙ (поиск по вопросу) ===\n" + "\n".join(
                f"- [{e['source']}, {(e['occurred_at'] or e['observed_at'])[:16]}, {e['sender'] or ''}] "
                f"{_clip((e['title'] or '') + ' ' + (e['body'] or ''), 300)}" for e in events))
    except Exception as e:
        parts.append(f"(поиск по журналу недоступен: {e.__class__.__name__})")

    parts += [
        f"=== АКТИВНЫЕ ЗАДАЧИ (id | название | предмет | срок) ===\n{_pending_tasks_block()}",
        f"=== РАСПИСАНИЕ НА 7 ДНЕЙ (код, точное) ===\n{_week_schedule()}",
        f"=== ДЕДЛАЙНЫ: СЕГОДНЯ + 3 ДНЯ ===\n{lists['deadlines']}",
        f"=== ПРОСРОЧЕНО ===\n{lists['overdue']}",
        f"=== ЛИЧНЫЕ НАПОМИНАНИЯ ===\n{lists['reminders']}",
    ]
    commitments = _commitments_block()
    if commitments:
        parts.append(f"=== ЕГО ОТКРЫТЫЕ ОБЕЩАНИЯ ===\n{commitments}")
    diary = _diary(3)
    if diary:
        parts.append(f"=== ДНЕВНИК ПОСЛЕДНИХ ДНЕЙ ===\n{diary}")
    mem = _memory()
    if mem:
        parts.append(f"=== ПАМЯТЬ О НЁМ ===\n{mem[:1800]}")
    study = _study_analysis(now)
    if study:
        parts.append(f"=== БАЛЛЫ И ПОСЕЩАЕМОСТЬ ===\n{study[:1500]}")
    return _cap(parts)
