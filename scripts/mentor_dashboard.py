"""
Окно наставника на рабочем столе — сборка и рендер data/mentor_feed.html.

Раньше ВСЁ содержимое окна (списки пар, дедлайнов, просрочек) писал Claude
текстом, а код только раскрашивал строки. Отсюда были ошибки, которые
промптом не лечились: модель брала у reminder_only-задачи поле deadline (это
момент напоминания, а не событие) и писала "01.10 — Приём ортодонта 2
октября" в "Срочно сегодня"; списки устаревали на часы после того, как
задача закрыта в боте; одна и та же просрочка то попадала, то нет.

Теперь:
  * списки — детерминированно из data/*.json в момент рендера (этот модуль);
    рендер дешёвый, его дёргает само окно (scripts/native_widget) при любом
    изменении tasks.json/reminders.json/расписания и при смене суток;
  * Claude в окне не участвует вообще (с 2026-10-02 по решению пользователя:
    выводы в пару предложений не стоили токенов) — 0 токенов на рендер.
"""
from __future__ import annotations

import datetime
import html as _html
import json
import os
import re

from config import UFA_TZ

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_DIR, "data")
FEED_HTML_FILE = os.path.join(DATA_DIR, "mentor_feed.html")

DEADLINE_HORIZON_DAYS = 3      # "Дедлайны (3 дня)": сегодня + 3 следующих дня
OVERDUE_VISIBLE = 5            # остальные просрочки — под спойлером
REMINDERS_VISIBLE = 7          # ближайшие напоминания видны, остальные — под спойлером
TOMORROW_TAB_TTL_SEC = 120     # вкладка "Завтра" сама возвращается на "Сегодня"

COLORS = {
    "schedule": "#8ab4f8",
    "deadlines": "#f4c869",
    "overdue": "#ff8a80",
    "reminders": "#a78bfa",
    "quote": "#c9a875",
}

# Стандартная сетка пар ТюмГУ — Modeus иногда отдаёт две пары подряд одной
# записью (17:40–20:50), показываем их двумя строками.
PAIR_SLOTS = [
    ("08:30", "10:00"), ("10:15", "11:45"), ("12:05", "13:35"), ("14:05", "15:35"),
    ("15:55", "17:25"), ("17:40", "19:10"), ("19:20", "20:50"), ("21:00", "22:30"),
]

_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
_WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


# ─── загрузка ────────────────────────────────────────────────────────

def _load(name: str, default):
    try:
        with open(os.path.join(DATA_DIR, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=UFA_TZ)


def _parse_dt(value) -> datetime.datetime | None:
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UFA_TZ)
    return dt.astimezone(UFA_TZ)


# ─── названия ────────────────────────────────────────────────────────

def short_course(course: str | None) -> str:
    """"3 семестр: Базы данных" → "Базы данных";
    "Дискретная математика 2 (3 семестр), группы ЛБ-17…" → "Дискретная математика 2"."""
    if not course:
        return ""
    c = re.sub(r"^\s*\d+\s*семестр:\s*", "", course)
    c = re.split(r"\s*[(,]", c, maxsplit=1)[0].strip()
    c = c.replace(" и операционные системы", " и ОС")
    return c


_DEADLINE_NOTE_RE = re.compile(
    r"\s*\((?:рекомендованн\w*\s+)?д[еe]?д?лайн[^)]*\)\s*", re.IGNORECASE,
)


def clean_title(title: str) -> str:
    """Убираем из названия дубль даты "(рекомендованный дедлайн 22.09.2026)" —
    дата и так стоит слева цветом."""
    t = _DEADLINE_NOTE_RE.sub(" ", title or "").strip()
    return re.sub(r"\s{2,}", " ", t) or (title or "").strip()


def is_soft_deadline(title: str) -> bool:
    return "рекомендован" in (title or "").lower()


def parse_event_date(title: str, ref: datetime.date) -> datetime.date | None:
    """Дата события из названия старых напоминаний, созданных до появления
    поля event_date: "Приём ортодонта 2 октября", "Оплатить 05.10"."""
    t = (title or "").lower()
    m = re.search(r"\b(\d{1,2})\s+(январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)", t)
    day = month = year = None
    if m:
        day = int(m.group(1))
        stem = m.group(2)
        month = _MONTHS.get(stem, _MONTHS.get(stem[:2]))
    else:
        m = re.search(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\b", t)
        if m:
            day, month = int(m.group(1)), int(m.group(2))
            if m.group(3):
                year = int(m.group(3))
                if year < 100:
                    year += 2000
    if not day or not month:
        return None
    try:
        if year:
            return datetime.date(year, month, day)
        d = datetime.date(ref.year, month, day)
        # "5 января", написанное в декабре, — это следующий год.
        if d < ref - datetime.timedelta(days=60):
            d = datetime.date(ref.year + 1, month, day)
        return d
    except ValueError:
        return None


def _fmt_interval(minutes: int, times_left: int) -> str:
    try:
        from reminders import format_interval
        return format_interval(minutes, times_left)
    except Exception:
        return f"каждые {minutes} мин" if minutes else "однократно"


# ─── задачи ──────────────────────────────────────────────────────────

def _task_item(t: dict, when: datetime.datetime) -> dict:
    return {
        "id": str(t.get("id")),
        "raw_title": (t.get("title") or "").strip(),
        "title": clean_title(t.get("title") or ""),
        "course": short_course(t.get("course_name")),
        "date": when,
        "soft": is_soft_deadline(t.get("title") or ""),
        "kind": "task",
        "url": t.get("url") or "",
    }


QUIET_FROM, QUIET_TO = 22, 8   # scheduler._in_quiet_hours: ночью напоминания не шлются


def _real_fire_time(next_at: datetime.datetime | None, now: datetime.datetime):
    """Когда напоминание реально придёт: просроченное — при ближайшей проверке
    (раз в 3-5 мин), попавшее в тихие часы — в 08:00. Иначе окно показывало
    бы "🔔 02:08", хотя бот ночью молчит."""
    if not next_at:
        return None
    t = max(next_at, now)
    if t.hour >= QUIET_FROM:
        t = (t + datetime.timedelta(days=1)).replace(hour=QUIET_TO, minute=0, second=0, microsecond=0)
    elif t.hour < QUIET_TO:
        t = t.replace(hour=QUIET_TO, minute=0, second=0, microsecond=0)
    return t


def build_reminders(tasks: list, reminders: list, now: datetime.datetime) -> list:
    """Все активные напоминания. Дата СОБЫТИЯ (event_date или из названия)
    и момент напоминания — разные поля, показываются раздельно."""
    # Отработавшие (times_left=0, exhausted) тоже показываем — напоминание
    # уходит только когда пользователь сам его закрыл (reminders.mark_sent).
    by_task: dict[str, list] = {}
    for r in reminders:
        by_task.setdefault(str(r.get("task_id")), []).append(r)

    items = []
    tasks_by_id = {str(t.get("id")): t for t in tasks}
    task_ids = [str(t.get("id")) for t in tasks if t.get("source") == "reminder_only" and not t.get("done")]
    # Напоминания, привязанные к обычным задачам (LMS/Нетология/ручные), — тоже.
    task_ids += [tid for tid in by_task if tid not in task_ids]

    for tid in task_ids:
        t = tasks_by_id.get(tid)
        if not t or t.get("done"):
            continue
        all_rems = by_task.get(tid, [])
        rems = [r for r in all_rems if r.get("times_left", 0) > 0]
        next_at = min((d for d in (_parse_dt(r.get("next_at")) for r in rems) if d), default=None)
        next_at = _real_fire_time(next_at, now)
        # Когда отработало в последний раз — для сортировки "ближайших".
        last_fired = max(
            (d for d in (_parse_dt(r.get("last_fired_at") or r.get("next_at")) for r in all_rems) if d),
            default=None,
        )
        one_off = bool(rems) and all(
            r.get("interval_minutes", 0) == 0 or r.get("times_left", 0) <= 1 for r in rems
        )
        event = None
        if t.get("event_date"):
            try:
                event = datetime.date.fromisoformat(str(t["event_date"])[:10])
            except ValueError:
                event = None
        if event is None and t.get("source") == "reminder_only":
            event = parse_event_date(t.get("title") or "", now.date())

        if rems:
            r0 = min(rems, key=lambda r: r.get("next_at") or "")
            interval = _fmt_interval(r0.get("interval_minutes", 0), r0.get("times_left", 0))
        else:
            interval = ""

        items.append({
            "id": tid,
            "raw_title": (t.get("title") or "").strip(),
            "title": clean_title(t.get("title") or ""),
            "course": short_course(t.get("course_name")) if t.get("source") != "reminder_only" else "",
            "event_date": event,
            "next_at": next_at,
            "one_off": one_off,
            "interval": interval,
            "is_personal": t.get("source") == "reminder_only",
            "kind": "reminder",
            "last_fired": last_fired,
        })
    # Ближайшие — первыми: отработавшие и ждущие закрытия (их время уже
    # пришло) по времени последнего срабатывания, затем звонящие по времени
    # следующего срабатывания.
    far = datetime.datetime.max.replace(tzinfo=UFA_TZ)
    items.sort(key=lambda i: (
        0 if i["next_at"] is None else 1,
        i["next_at"] or i["last_fired"] or far,
    ))
    return items


def build_deadlines(tasks: list, reminder_items: list, now: datetime.datetime) -> tuple[list, list]:
    """(дедлайны на горизонте, просроченные). Просрочка — только у настоящих
    задач (lms/netology/manual), не у личных напоминаний."""
    today = now.date()
    horizon = today + datetime.timedelta(days=DEADLINE_HORIZON_DAYS)
    upcoming, overdue = [], []
    for t in tasks:
        if t.get("done") or t.get("source") == "reminder_only":
            continue
        dl = _parse_dt(t.get("deadline"))
        if not dl:
            continue
        if dl < now:
            overdue.append(_task_item(t, dl))
        elif dl.date() <= horizon:
            upcoming.append(_task_item(t, dl))

    # Личные напоминания попадают в дедлайны ПО ДАТЕ СОБЫТИЯ. Если даты
    # события нет, а напоминание разовое — его день и есть "когда сделать".
    # Повторяющиеся без даты события ("пить воду") дедлайном не являются.
    for r in reminder_items:
        if not r["is_personal"]:
            continue
        if r["event_date"]:
            day = r["event_date"]
        elif r["one_off"] and r["next_at"]:
            day = r["next_at"].date()
        else:
            continue
        if today <= day <= horizon:
            upcoming.append({
                "id": r["id"], "raw_title": r["raw_title"], "title": r["title"], "course": "",
                "date": datetime.datetime.combine(day, datetime.time(23, 59), tzinfo=UFA_TZ),
                "soft": False, "kind": "reminder", "url": "",
            })

    upcoming.sort(key=lambda i: (i["date"], i["title"]))
    overdue.sort(key=lambda i: (i["date"], i["title"]))   # самые старые — первыми
    return upcoming, overdue


# ─── расписание ──────────────────────────────────────────────────────

def _yac_links(day: datetime.date) -> dict:
    """HH:MM → ссылка на вебинар на этот день из data/yac_schedule.json
    (обновляется каждые 30 минут scripts/refresh_yac_links.py)."""
    data = _load("yac_schedule.json", {})
    try:
        age_h = (_now() - _parse_dt(data.get("fetched_at"))).total_seconds() / 3600
        if age_h > 12:
            return {}
    except Exception:
        return {}
    iso = day.isoformat()
    links = {}
    for ev in data.get("modeus_events", []):
        start = ev.get("start") or ""
        if start.startswith(iso) and ev.get("mts_link"):
            links[start[11:16]] = ev["mts_link"]
    for ev in data.get("netology_webinars", []):
        starts_at = ev.get("starts_at") or ""
        if starts_at.startswith(iso) and ev.get("webinar_url"):
            links.setdefault(starts_at[11:16], ev["webinar_url"])
    return links


def _yac_lxp_ids() -> set:
    data = _load("yac_schedule.json", {})
    return {ev.get("id") for ev in data.get("modeus_events", []) if ev.get("is_lxp")}


def _split_pairs(start: datetime.datetime, end: datetime.datetime) -> list:
    """Запись Modeus на две пары подряд → две строки по слотам сетки."""
    if not end or end <= start:
        return [(start, start + datetime.timedelta(minutes=90))]
    slots = []
    for s, e in PAIR_SLOTS:
        sh, sm = map(int, s.split(":"))
        eh, em = map(int, e.split(":"))
        ss = start.replace(hour=sh, minute=sm, second=0, microsecond=0)
        ee = start.replace(hour=eh, minute=em, second=0, microsecond=0)
        if ss >= start and ee <= end:
            slots.append((ss, ee))
    return slots if len(slots) >= 2 else [(start, end)]


def build_schedule(day: datetime.date) -> dict:
    iso = day.isoformat()
    lines, notes = [], []

    digest = _load("schedule_vk_digest.json", {})
    if digest.get("date") == iso and digest.get("items"):
        # Сводка из беседы ВК — полнее Modeus (вебинары на my.mts-link.ru).
        for it in digest["items"]:
            is_lxp = "асинхрон" in (it.get("time", "") + it.get("format", "")).lower()
            start = None
            if not is_lxp and re.match(r"^\d{1,2}:\d{2}$", it.get("time", "")):
                h, m = map(int, it["time"].split(":"))
                start = datetime.datetime.combine(day, datetime.time(h, m), tzinfo=UFA_TZ)
            label = it.get("subject", "")
            if it.get("topic"):
                label += f": {it['topic']}"
            lines.append({
                "time": "LXP" if is_lxp else (it.get("time") or "?"),
                "label": label, "url": it.get("url") or "",
                "start": start, "end": start + datetime.timedelta(minutes=90) if start else None,
            })
    else:
        cache = _load("schedule_cache.json", {})
        lxp_ids = _yac_lxp_ids()
        links = _yac_links(day)
        for week in cache.values():
            if not isinstance(week, dict):
                continue
            for src in ("data", "netology"):
                for lesson in (week.get(src) or {}).get(iso) or []:
                    start = _parse_dt(lesson.get("start"))
                    end = _parse_dt(lesson.get("end"))
                    if not start:
                        continue
                    label = short_course(lesson.get("course_name"))
                    name = (lesson.get("name") or "").strip()
                    if name:
                        label = f"{label}: {name}" if label else name
                    loc = lesson.get("location") or ""
                    is_lxp = loc == "LXP" or lesson.get("id") in lxp_ids
                    if is_lxp:
                        lines.append({"time": "LXP", "label": label, "url": "", "start": None, "end": None})
                        continue
                    if src == "netology":
                        spans = [(start, start + datetime.timedelta(minutes=90))]
                    else:
                        spans = _split_pairs(start, end)
                    for s, e in spans:
                        hhmm = s.strftime("%H:%M")
                        url = links.get(hhmm) or (loc if src == "netology" and loc.startswith("http") else "")
                        lines.append({"time": hhmm, "label": label, "url": url, "start": s, "end": e})

    timed = sorted((l for l in lines if l["start"]), key=lambda l: l["start"])
    lxp = [l for l in lines if not l["start"]]

    # Переносы/отмены из беседы ВК (scripts/mentor_checkin.py пишет короткие
    # факты "Предмет ДД.ММ отменён") — показываем предупреждением под днём.
    ddmm = day.strftime("%d.%m")
    now = _now()
    for o in _load("schedule_overrides.json", []):
        note = o.get("note") or ""
        added = _parse_dt(o.get("added_at"))
        if ddmm in note and added and (now - added).days < 4:
            notes.append(note)

    return {"date": day, "lines": timed + lxp, "notes": notes}


# ─── модель целиком ─────────────────────────────────────────────────

def build_model(now: datetime.datetime | None = None) -> dict:
    now = now or _now()
    tasks = _load("tasks.json", [])
    reminders = _load("reminders.json", [])
    reminder_items = build_reminders(tasks, reminders, now)
    upcoming, overdue = build_deadlines(tasks, reminder_items, now)
    try:
        import sys
        sys.path.insert(0, os.path.join(PROJECT_DIR, "scripts"))
        from mentor_checkin import get_daily_quote
        quote = get_daily_quote()
    except Exception:
        quote = ""
    model = {
        "now": now,
        "data_updated_at": _data_updated_at(),
        "today": build_schedule(now.date()),
        "tomorrow": build_schedule(now.date() + datetime.timedelta(days=1)),
        "deadlines": upcoming,
        "overdue": overdue,
        "reminders": reminder_items,
        "quote": quote,
    }
    return model


def _data_updated_at() -> datetime.datetime | None:
    """Когда последний раз реально менялись данные окна (задачи, расписание,
    ссылки) — это и показываем как «обновлено» в заголовке."""
    times = []
    for name in ("tasks.json", "reminders.json", "schedule_cache.json", "yac_schedule.json"):
        try:
            times.append(os.path.getmtime(os.path.join(DATA_DIR, name)))
        except OSError:
            pass
    if not times:
        return None
    return datetime.datetime.fromtimestamp(max(times), tz=UFA_TZ)


# ─── текст для промпта и «Напоминаний» macOS ────────────────────────

def _sched_text(s: dict) -> list:
    out = [f"{l['time']} — {l['label']}" for l in s["lines"]] or ["(пар нет)"]
    out += [f"⚠ {n}" for n in s["notes"]]
    return out


def _item_text(i: dict) -> str:
    course = f" ({i['course']})" if i.get("course") else ""
    soft = " [рекомендованный срок]" if i.get("soft") else ""
    personal = " [личное напоминание]" if i.get("kind") == "reminder" else ""
    return f"{i['date'].strftime('%d.%m')} — {i['title']}{course}{soft}{personal}"


def model_as_text(model: dict) -> str:
    """Те же списки, что видит пользователь, — для промпта (Claude пишет к
    ним только выводы) и для списка «Наставник» в приложении «Напоминания»."""
    parts = [
        "Расписание сегодня:\n" + "\n".join(_sched_text(model["today"])),
        "Расписание завтра:\n" + "\n".join(_sched_text(model["tomorrow"])),
        "Дедлайны (сегодня + 3 дня):\n" + ("\n".join(_item_text(i) for i in model["deadlines"]) or "(нет)"),
        "Просрочено:\n" + ("\n".join(_item_text(i) for i in model["overdue"]) or "(нет)"),
    ]
    rems = []
    for r in model["reminders"]:
        ev = f"событие {r['event_date'].strftime('%d.%m')}; " if r["event_date"] else ""
        nxt = f"напомню {r['next_at'].strftime('%d.%m %H:%M')}" if r["next_at"] else "отработало, ждёт закрытия"
        rems.append(f"{r['title']} ({ev}{nxt}, {r['interval']})")
    parts.append("Напоминания:\n" + ("\n".join(rems) or "(нет)"))
    return "\n\n".join(parts)


# ─── HTML ────────────────────────────────────────────────────────────

def _e(s) -> str:
    return _html.escape(str(s or ""), quote=True)


def _sched_pane(s: dict, empty_text: str) -> str:
    parts = []
    for l in s["lines"]:
        link = f'<a class="list-link" href="{_e(l["url"])}">Ссылка</a>' if l["url"] else ""
        data = ""
        if l["start"]:
            data = f' data-start="{_e(l["start"].isoformat())}" data-end="{_e(l["end"].isoformat())}"'
        parts.append(
            f'<div class="list-line sched-line"{data}><span class="list-time">{_e(l["time"])}</span>'
            f'<span class="list-body">{_e(l["label"])}{link}</span></div>'
        )
    if not s["lines"]:
        parts.append(f'<div class="comment-line">{_e(empty_text)}</div>')
    for n in s["notes"]:
        parts.append(f'<div class="note-line">⚠ {_e(n)}</div>')
    return "".join(parts)


def _check_line(i: dict, date_label: str, extra_class: str = "", meta: str = "") -> str:
    style = f' style="--accent:{COLORS["reminders"]}"' if i["kind"] == "reminder" else ""
    course = f' <span class="course">({_e(i["course"])})</span>' if i.get("course") else ""
    soft = '<span class="tag">рек.</span>' if i.get("soft") else ""
    meta_html = f'<span class="meta">{_e(meta)}</span>' if meta else ""
    return (
        f'<div class="list-line task-line {extra_class}"{style} data-id="{_e(i["id"])}" '
        f'data-title="{_e(i["raw_title"])}">'
        f'<button class="check" tabindex="-1" title="Отметить выполненным"></button>'
        f'<span class="list-time">{_e(date_label)}</span>'
        f'<span class="list-body">{_e(i["title"])}{course}{soft}{meta_html}'
        f'<span class="confirm-hint">ещё раз — закрыть</span></span></div>'
    )


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _spoiler(key: str, label: str, body_html: str) -> str:
    """Красивый спойлер; открыт/закрыт — переживает перезагрузку окна."""
    return (
        f'<details class="more" data-key="{_e(key)}"><summary><span class="more-pill">'
        f'{_e(label)}<span class="chev"></span></span></summary>'
        f'<div class="more-body">{body_html}</div></details>'
    )


def _category(key: str, title_html: str, body: str) -> str:
    return (
        f'<div class="category cat-{key}" style="--accent:{COLORS[key]}">'
        f'<div class="category-title">{title_html}</div>{body}</div>'
    )


def render_html(model: dict) -> str:
    now = model["now"]
    tomorrow = model["tomorrow"]["date"]

    sched_title = (
        'Расписание<span class="tabs">'
        '<button class="tab on" data-tab="today" tabindex="-1">Сегодня</button>'
        f'<button class="tab" data-tab="tomorrow" tabindex="-1">Завтра · {_WEEKDAYS[tomorrow.weekday()]}</button>'
        '</span>'
    )
    sched_body = (
        f'<div class="pane" data-pane="today">{_sched_pane(model["today"], "Сегодня пар нет.")}</div>'
        f'<div class="pane" data-pane="tomorrow" hidden>'
        f'{_sched_pane(model["tomorrow"], "Завтра пар нет.")}</div>'
    )
    blocks = [_category("schedule", sched_title, sched_body)]

    # В этом блоке слева — когда напоминание сработает (блок так и называется),
    # дата самого события — отдельной явной пометкой "событие ДД.ММ", чтобы
    # они больше никогда не путались (см. историю с "Приём ортодонта").
    rem_lines = []
    for r in model["reminders"]:
        if r["next_at"]:
            when = r["next_at"]
            label = when.strftime("%d.%m")
            meta = f"🔔 {when.strftime('%H:%M')}"
            if r["interval"] and not r["one_off"]:
                meta += f" · {r['interval']}"
        else:
            label = r["last_fired"].strftime("%d.%m") if r["last_fired"] else "—"
            meta = "отработало · ждёт, пока закроешь"
        if r["event_date"]:
            meta = f"событие {r['event_date'].strftime('%d.%m')} · " + meta
        item = dict(r, date=None, soft=False)
        rem_lines.append(_check_line(item, label, meta=meta))
    if not rem_lines:
        rem_lines = ['<div class="comment-line">Активных напоминаний нет.</div>']
    rem_html = "".join(rem_lines[:REMINDERS_VISIBLE])
    if len(rem_lines) > REMINDERS_VISIBLE:
        n = len(rem_lines) - REMINDERS_VISIBLE
        rem_html += _spoiler("reminders", f"Ещё {n} {_plural(n, 'напоминание', 'напоминания', 'напоминаний')}",
                             "".join(rem_lines[REMINDERS_VISIBLE:]))
    title = "Напоминания" + (f' <span class="count">{len(model["reminders"])}</span>' if model["reminders"] else "")
    blocks.append(_category("reminders", title, rem_html))



    dl_lines = [_check_line(i, i["date"].strftime("%d.%m")) for i in model["deadlines"]]
    if not dl_lines:
        dl_lines = ['<div class="comment-line">На ближайшие три дня сроков нет — горизонт чист.</div>']
    blocks.append(_category("deadlines", "Дедлайны (3 дня)", "".join(dl_lines)))

    overdue = model["overdue"]
    if overdue:
        visible = "".join(_check_line(i, i["date"].strftime("%d.%m")) for i in overdue[:OVERDUE_VISIBLE])
        hidden = overdue[OVERDUE_VISIBLE:]
        more = ""
        if hidden:
            more = _spoiler(
                "overdue", f"Ещё {len(hidden)} просроченн{'ая' if len(hidden) == 1 else 'ых'}",
                "".join(_check_line(i, i["date"].strftime("%d.%m")) for i in hidden),
            )
        title = f'Просрочено <span class="count">{len(overdue)}</span>'
        blocks.append(_category("overdue", title, visible + more))

    if model["quote"]:
        blocks.append(_category("quote", "Цитата дня", f'<div class="comment-line quote-line">{_e(model["quote"])}</div>'))

    upd = model["data_updated_at"]
    if upd is None:
        updated = ""
    elif upd.date() == now.date():
        updated = f"данные {upd.strftime('%H:%M')}"
    else:
        updated = f"данные {upd.strftime('%d.%m %H:%M')}"

    return PAGE_TEMPLATE.replace("{{UPDATED}}", _e(updated)) \
        .replace("{{BODY}}", "".join(blocks)) \
        .replace("{{TOMORROW_TTL_MS}}", str(TOMORROW_TAB_TTL_SEC * 1000))


def render_feed(now: datetime.datetime | None = None) -> bool:
    """Пересобрать data/mentor_feed.html. Пишем ТОЛЬКО если содержимое
    изменилось — иначе окно перезагружалось бы (и мигало) на каждый
    ничего не меняющий проход refresh_yac_links / сторожа."""
    html_doc = render_html(build_model(now))
    try:
        with open(FEED_HTML_FILE, encoding="utf-8") as f:
            if f.read() == html_doc:
                return False
    except FileNotFoundError:
        pass
    tmp = FEED_HTML_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html_doc)
    os.replace(tmp, FEED_HTML_FILE)
    return True


PAGE_TEMPLATE = r'''<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Наставник</title>
<style>
  html, body { margin:0; padding:0; background: transparent; height:100%; }
  body {
    font-family: -apple-system, "SF Pro Text", "Helvetica Neue", sans-serif;
    color: rgba(255,255,255,0.92);
    -webkit-user-select: none;
    box-sizing: border-box;
    background: rgba(20, 22, 26, 0.16);
    backdrop-filter: blur(14px) saturate(110%);
    -webkit-backdrop-filter: blur(14px) saturate(110%);
    text-shadow: 0 1px 3px rgba(0,0,0,0.5);
    overflow-y: auto; overflow-x: hidden;
    cursor: default;
  }
  body::-webkit-scrollbar { width: 6px; }
  body::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.15); border-radius: 3px; }
  .header {
    display:flex; align-items:baseline; justify-content:space-between;
    padding: 16px 20px 12px;
    border-bottom: 1px solid rgba(255,255,255,0.08);
  }
  .header .name {
    font-size: 14.5px; font-weight: 500; letter-spacing: 0.06em; text-transform: uppercase;
    color: rgba(255,255,255,0.55);
  }
  .header .right { display:flex; align-items:center; gap:10px; }
  .refresh {
    display:none; align-items:center; gap:5px; font: inherit; font-size: 11.5px; font-weight:500;
    padding: 3px 9px; border-radius: 8px; cursor: pointer; border: 1px solid rgba(255,255,255,0.12);
    background: rgba(255,255,255,0.06); color: rgba(255,255,255,0.6); transition: background .15s, color .15s;
  }
  body:not(.no-actions) .refresh { display:inline-flex; }
  .refresh:hover { background: rgba(255,255,255,0.12); color: rgba(255,255,255,0.9); }
  .refresh.busy { pointer-events:none; color: #8ab4f8; }
  .refresh.busy svg { animation: spin 1s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .header .updated { font-size: 11px; color: rgba(255,255,255,0.32); font-variant-numeric: tabular-nums; }
  .content { padding: 14px 20px 20px; }
  .category {
    margin-bottom: 18px; padding-left: 12px;
    border-left: 2px solid var(--accent, rgba(255,255,255,0.3));
  }
  .category:last-child { margin-bottom: 0; }
  .category-title {
    display:flex; align-items:center; gap:10px;
    font-size: 12.5px; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase;
    color: var(--accent, rgba(255,255,255,0.55)); margin-bottom: 8px;
  }
  .category-title .count {
    font-size: 11px; font-weight:600; padding: 0 6px; border-radius: 8px; letter-spacing:0;
    background: color-mix(in srgb, var(--accent) 18%, transparent);
  }
  .tabs { display:inline-flex; gap:2px; padding:2px; border-radius:8px; background: rgba(255,255,255,0.06); text-transform:none; letter-spacing:0; }
  .tab {
    font: inherit; font-size: 11.5px; font-weight: 500; border:0; cursor:pointer;
    padding: 2px 9px; border-radius: 6px; background: transparent; color: rgba(255,255,255,0.5);
  }
  .tab.on { background: color-mix(in srgb, var(--accent) 22%, transparent); color: var(--accent); }
  .list-line { display: flex; align-items: baseline; gap: 8px; font-size: 15px; line-height: 1.5; margin-bottom: 4px; transition: opacity .25s; }
  .list-time {
    color: var(--accent, rgba(255,255,255,0.7)); font-weight: 600;
    font-variant-numeric: tabular-nums; flex-shrink: 0; min-width: 40px;
  }
  .list-body { color: rgba(255,255,255,0.9); font-weight: 300; }
  .course { color: rgba(255,255,255,0.6); }
  .tag { font-size: 10.5px; margin-left: 6px; padding: 0 5px; border-radius: 5px; color: rgba(255,255,255,0.45); border: 1px solid rgba(255,255,255,0.15); vertical-align: 1px; }
  .meta { font-size: 12px; margin-left: 8px; color: rgba(255,255,255,0.42); white-space: nowrap; }
  .list-link {
    color: var(--accent, #8ab4f8); font-size: 12px; margin-left: 8px;
    text-decoration: none; border-bottom: 1px solid currentColor; opacity: 0.85;
  }
  .list-link:hover { opacity: 1; }
  .sched-line.now .list-time::before { content:""; display:inline-block; width:6px; height:6px; border-radius:50%; background: var(--accent); margin-right:5px; vertical-align: 2px; box-shadow: 0 0 6px var(--accent); }
  .comment-line {
    font-size: 14px; font-style: italic; color: rgba(255,255,255,0.55);
    line-height: 1.45; margin-top: 6px;
  }
  .note-line { font-size: 13.5px; color: #f4c869; margin-top: 6px; }
  .quote-line { font-size: 15px; color: rgba(255,255,255,0.8); }

  /* ── галочки ── */
  .check {
    flex-shrink:0; align-self: center; width: 15px; height: 15px; padding:0; margin: 0 -2px 0 0;
    border-radius: 50%; border: 1.5px solid rgba(255,255,255,0.28); background: transparent;
    cursor: pointer; position: relative; transition: border-color .15s, background .15s, transform .15s;
  }
  .no-actions .check { display:none; }
  .check:hover { border-color: var(--accent); }
  .task-line.armed .check { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 25%, transparent); transform: scale(1.12); animation: pulse 0.9s ease-in-out infinite; }
  @keyframes pulse { 50% { box-shadow: 0 0 0 4px color-mix(in srgb, var(--accent) 25%, transparent); } }
  .confirm-hint { display:none; font-size: 11.5px; margin-left: 8px; color: var(--accent); font-style: italic; }
  .task-line.armed .confirm-hint { display:inline; }
  .task-line.pending .check { background: var(--accent); border-color: var(--accent); }
  .task-line.pending .check::after { content:""; position:absolute; left:4px; top:1px; width:4px; height:7px; border: solid #15171b; border-width: 0 1.5px 1.5px 0; transform: rotate(45deg); }
  .task-line.pending .list-body { text-decoration: line-through; opacity: .5; }
  .task-line.gone { display:none; }

  /* ── спойлер просрочек ── */
  details.more summary { list-style:none; cursor:pointer; margin: 6px 0 2px; }
  details.more summary::-webkit-details-marker { display:none; }
  .more-pill {
    display:inline-flex; align-items:center; gap:6px; font-size: 12px; font-weight: 500;
    padding: 3px 10px; border-radius: 10px; color: var(--accent);
    background: color-mix(in srgb, var(--accent) 12%, transparent);
    border: 1px dashed color-mix(in srgb, var(--accent) 45%, transparent);
  }
  .more-pill:hover { background: color-mix(in srgb, var(--accent) 20%, transparent); }
  .chev { width:6px; height:6px; border: solid currentColor; border-width: 0 1.5px 1.5px 0; transform: rotate(45deg) translateY(-2px); transition: transform .2s; }
  details.more[open] .chev { transform: rotate(-135deg) translateY(-1px); }
  details.more[open] .more-body { animation: reveal .25s ease-out; }
  @keyframes reveal { from { opacity:0; transform: translateY(-4px); } }
  .more-body { padding-top: 4px; }

  /* ── тост подтверждения/отмены ── */
  .toast {
    position: fixed; left: 50%; bottom: 18px; transform: translate(-50%, 20px); opacity: 0;
    display:flex; align-items:center; gap: 12px; max-width: 80%;
    padding: 9px 14px; border-radius: 12px; font-size: 13px;
    background: rgba(28,30,36,0.92); border: 1px solid rgba(255,255,255,0.12);
    box-shadow: 0 8px 24px rgba(0,0,0,0.35); transition: opacity .2s, transform .2s; pointer-events:none;
  }
  .toast.show { opacity: 1; transform: translate(-50%, 0); pointer-events:auto; }
  .toast .t-text { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .toast button { font: inherit; font-weight:600; border:0; cursor:pointer; border-radius: 8px; padding: 3px 10px; background: rgba(255,255,255,0.12); color: #fff; }
  .toast.error { border-color: rgba(255,69,58,0.5); color: rgb(255,138,128); }
</style>
</head>
<body class="no-actions">
  <div class="header"><span class="name">Наставник</span><span class="right"><span class="updated">{{UPDATED}}</span><button class="refresh" id="refresh" tabindex="-1" title="Обновить всё: задачи LMS и Нетологии, расписание, ссылки на вебинары"><svg viewBox="0 0 16 16" width="13" height="13"><path d="M13.6 8a5.6 5.6 0 1 1-1.64-3.96" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><path d="M12.4 1.6v3h-3" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg><span class="r-label">Обновить</span></button></span></div>
  <div class="content">{{BODY}}</div>
  <div class="toast" id="toast"><span class="t-text"></span><button hidden>Отменить</button></div>
<script>
(function () {
  const TOMORROW_TTL = {{TOMORROW_TTL_MS}};
  const ARM_MS = 3500, MIN_SECOND_CLICK_MS = 450, UNDO_MS = 5000;
  const ss = {
    get(k) { try { return JSON.parse(sessionStorage.getItem(k)); } catch (e) { return null; } },
    set(k, v) { try { sessionStorage.setItem(k, JSON.stringify(v)); } catch (e) {} },
  };
  const bridge = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.mentor;
  if (bridge && window.__mentorNonce) document.body.classList.remove("no-actions");

  // ── вкладки Сегодня/Завтра ──
  let tabTimer = null;
  function showTab(name, remainingMs) {
    document.querySelectorAll(".tab").forEach(b => b.classList.toggle("on", b.dataset.tab === name));
    document.querySelectorAll(".pane").forEach(p => p.hidden = p.dataset.pane !== name);
    clearTimeout(tabTimer);
    if (name === "tomorrow") {
      tabTimer = setTimeout(() => showTab("today"), remainingMs ?? TOMORROW_TTL);
      if (remainingMs == null) ss.set("tab", { name, at: Date.now() });
    } else {
      ss.set("tab", null);
    }
  }
  document.querySelectorAll(".tab").forEach(b => b.addEventListener("click", () => showTab(b.dataset.tab)));
  const savedTab = ss.get("tab");
  if (savedTab && savedTab.name === "tomorrow") {
    const left = TOMORROW_TTL - (Date.now() - savedTab.at);
    if (left > 0) showTab("tomorrow", left);
  }

  // ── спойлер и прокрутка переживают перезагрузку страницы ──
  document.querySelectorAll("details.more").forEach(d => {
    if (ss.get("open:" + d.dataset.key)) d.open = true;
    d.addEventListener("toggle", () => ss.set("open:" + d.dataset.key, d.open));
  });
  const savedScroll = ss.get("scroll");
  if (savedScroll) document.scrollingElement.scrollTop = savedScroll;
  window.addEventListener("scroll", () => ss.set("scroll", document.scrollingElement.scrollTop), { passive: true });

  // ── текущая пара отмечается точкой; прошедшие НЕ скрываются и не тускнеют ──
  function markNow() {
    const now = Date.now();
    document.querySelectorAll('[data-pane="today"] .sched-line[data-start]').forEach(l => {
      const s = Date.parse(l.dataset.start), e = Date.parse(l.dataset.end);
      l.classList.toggle("now", now >= s && now < e);
    });
  }
  markNow(); setInterval(markNow, 30000);

  // ── галочки: два клика (с паузой), затем 5 с на отмену, только потом запись ──
  const toast = document.getElementById("toast");
  const tText = toast.querySelector(".t-text"), tBtn = toast.querySelector("button");
  let toastTimer = null;
  function showToast(text, opts) {
    opts = opts || {};
    tText.textContent = text;
    toast.classList.toggle("error", !!opts.error);
    tBtn.hidden = !opts.onUndo;
    tBtn.onclick = opts.onUndo || null;
    toast.classList.add("show");
    clearTimeout(toastTimer);
    if (opts.ttl) toastTimer = setTimeout(() => toast.classList.remove("show"), opts.ttl);
  }
  function hideToast() { toast.classList.remove("show"); }

  let armed = null, armedAt = 0, armTimer = null;
  let pending = null;   // {line, timer, tick, req}
  let reqSeq = 0;

  function disarm() {
    if (armed) armed.classList.remove("armed");
    armed = null; clearTimeout(armTimer);
  }
  function arm(line) {
    disarm();
    armed = line; armedAt = Date.now();
    line.classList.add("armed");
    armTimer = setTimeout(disarm, ARM_MS);
  }
  function startPending(line) {
    const title = line.querySelector(".list-body").firstChild.textContent.trim();
    line.classList.add("pending");
    let left = Math.round(UNDO_MS / 1000);
    const p = { line, req: null };
    const undo = () => {
      clearTimeout(p.timer); clearInterval(p.tick);
      line.classList.remove("pending"); pending = null;
      showToast("Отменено", { ttl: 1500 });
    };
    const render = () => showToast(`Закрываю «${title}» · ${left}`, { onUndo: undo });
    render();
    p.tick = setInterval(() => { left -= 1; if (left > 0) render(); }, 1000);
    p.timer = setTimeout(() => {
      clearInterval(p.tick);
      p.req = String(++reqSeq);
      showToast("Сохраняю…");
      bridge.postMessage({
        action: "complete", id: line.dataset.id, title: line.dataset.title,
        req: p.req, nonce: window.__mentorNonce,
      });
    }, UNDO_MS);
    pending = p;
  }
  window.mentorActionResult = function (req, ok, message) {
    if (!pending || pending.req !== String(req)) return;
    const line = pending.line; pending = null;
    if (ok) {
      line.classList.add("gone");
      document.querySelectorAll(`.task-line[data-id="${CSS.escape(line.dataset.id)}"]`).forEach(l => l.classList.add("gone"));
      showToast("Готово ✓ · вернуть можно кнопкой в Telegram", { ttl: 3000 });
    } else {
      line.classList.remove("pending");
      showToast("Не закрыто: " + (message || "ошибка"), { error: true, ttl: 5000 });
    }
  };
  // Окно перезагружает страницу при изменении данных — но не посреди
  // подтверждения/отсчёта отмены (иначе действие потерялось бы молча).
  window.mentorCanReload = function () { return !armed && !pending; };

  // ── кнопка «Обновить» ──
  const refreshBtn = document.getElementById("refresh");
  const rLabel = refreshBtn.querySelector(".r-label");
  if (ss.get("refreshing") && Date.now() - ss.get("refreshing") < 300000) {
    refreshBtn.classList.add("busy"); rLabel.textContent = "Обновляю…";
  }
  refreshBtn.addEventListener("click", () => {
    if (!bridge || !window.__mentorNonce || refreshBtn.classList.contains("busy")) return;
    refreshBtn.classList.add("busy"); rLabel.textContent = "Обновляю…";
    ss.set("refreshing", Date.now());
    bridge.postMessage({ action: "refresh", req: String(++reqSeq), nonce: window.__mentorNonce });
  });
  // Ответ может прийти уже в перезагруженную страницу (данные обновились
  // по ходу) — поэтому без сверки req.
  window.mentorRefreshResult = function (ok, message) {
    ss.set("refreshing", null);
    refreshBtn.classList.remove("busy"); rLabel.textContent = "Обновить";
    showToast(message || (ok ? "Обновлено" : "Не удалось обновить"), { error: !ok, ttl: ok ? 3500 : 6000 });
  };

  document.addEventListener("click", ev => {
    const btn = ev.target.closest(".check");
    if (!btn) { if (armed && !ev.target.closest(".task-line.armed")) disarm(); return; }
    if (!bridge || !window.__mentorNonce) return;
    const line = btn.closest(".task-line");
    if (pending) { showToast("Подожди — предыдущее ещё не сохранено", { ttl: 2000 }); return; }
    if (armed !== line) { arm(line); return; }
    if (Date.now() - armedAt < MIN_SECOND_CLICK_MS) return;   // двойной клик ≠ подтверждение
    disarm();
    startPending(line);
  });
  document.addEventListener("keydown", ev => { if (ev.key === "Escape") disarm(); });
})();
</script>
</body></html>
'''
