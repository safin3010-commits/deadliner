import datetime
from config import UFA_TZ

MONTH_NAMES = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря"
]
DAY_NAMES = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

PAIR_SLOTS = [
    (datetime.time(8, 30),  datetime.time(10, 0)),
    (datetime.time(10, 15), datetime.time(11, 45)),
    (datetime.time(12, 5),  datetime.time(13, 35)),
    (datetime.time(14, 5),  datetime.time(15, 35)),
    (datetime.time(15, 55), datetime.time(17, 25)),
    (datetime.time(17, 40), datetime.time(19, 10)),
    (datetime.time(19, 20), datetime.time(20, 50)),
    (datetime.time(21, 0),  datetime.time(22, 30)),
]

SOURCE_EMOJI = {
    "lms": "🎓",
    "netology": "📖",
    "manual": "✏️",
    "mail": "📧",
    "messenger": "💬",
}

SOURCE_NAME = {
    "lms": "LMS",
    "netology": "Нетология",
    "manual": "Вручную",
    "mail": "Почта",
    "messenger": "Мессенджер",
}

# Короткие названия курсов
COURSE_SHORT = {
    "Анализ данных (Анализ данных в Low-code платформах) (электив": "Анализ данных",
    "2026_Компьютерные_сети": "Компьютерные сети",
    "Дискретная математика - 1 (2 семестр) 2026": "Дискретная математика",
    "Дискретная математика (core HARD 2 семестр 2025-2026)": "Дискретная математика",
    "Математический анализ (hard, 2025-2026)": "Математический анализ",
    "Правоведение (lxp) (элективы 1 курс 2 семестр ОФО 2025-2026)": "Правоведение",
    "Физическая культура и спорт: теория и методика двигательной": "Физкультура",
    "Техника безопасности (Физическая культура)": "Техника безопасности",
    "Философия: технологии мышления. Мастерская А. И. Павловского": "Философия",
}


def _s(val) -> str:
    return (val or "").strip()


def _sl(val) -> str:
    return _s(val).lower()


def _short_course(course: str, title: str = "") -> str:
    """Укорачиваем длинные названия курсов."""
    course = _s(course) or _s(title)[:30]
    for long, short in COURSE_SHORT.items():
        if course.startswith(long[:30]):
            return short
    # Если длиннее 30 символов — обрезаем
    if len(course) > 30:
        return course[:28] + "…"
    return course


def _deadline_emoji(deadline_str: str | None) -> str:
    if not deadline_str:
        return "📌"
    try:
        dt = datetime.datetime.fromisoformat(deadline_str).astimezone(UFA_TZ)
        days = (dt - datetime.datetime.now(tz=UFA_TZ)).days
        if days <= 2:
            return "🔴"
        elif days <= 7:
            return "🟡"
        else:
            return "🟢"
    except Exception:
        return "📌"


def _format_date(deadline_str: str | None) -> str:
    if not deadline_str:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(deadline_str).astimezone(UFA_TZ)
        now = datetime.datetime.now(tz=UFA_TZ)
        today = now.date()
        deadline_date = dt.date()
        days = (deadline_date - today).days
        date = f"{dt.day} {MONTH_NAMES[dt.month - 1]}"
        if days == 0:
            return f"{date} — сегодня!"
        elif days == 1:
            return f"{date} — завтра"
        elif days < 0:
            return f"{date} — просрочено"
        else:
            return date
    except Exception:
        return ""


def _get_filtered_tasks(tasks: list, filter_type: str) -> list:
    """Возвращает список незавершённых задач. Ручные (manual) — всегда выше."""
    pending = [t for t in tasks if not t.get("done")]
    now = datetime.datetime.now(tz=UFA_TZ)

    def days_left(t):
        try:
            return (datetime.datetime.fromisoformat(t["deadline"]).astimezone(UFA_TZ) - now).days
        except Exception:
            return 9999

    def sort_key(t):
        # Ручные задачи первыми (0), остальные (1)
        manual_first = 0 if t.get("source") == "manual" else 1
        deadline = t.get("deadline", "9999")
        return (manual_first, deadline)

    if filter_type == "urgent":
        filtered = [t for t in pending if t.get("deadline") and -3 <= days_left(t) <= 2]
        return sorted(filtered, key=sort_key)
    elif filter_type == "all":
        # Просроченные — не старше 3 дней, остальные — все
        with_date = [t for t in pending if t.get("deadline") and days_left(t) >= -10]
        without_date = [t for t in pending if not t.get("deadline")]
        # Ручные с датой первыми, потом остальные с датой, потом без даты
        manual_with = sorted([t for t in with_date if t.get("source") == "manual"], key=lambda t: t.get("deadline", ""))
        other_with = sorted([t for t in with_date if t.get("source") != "manual"], key=lambda t: t.get("deadline", ""))
        manual_without = [t for t in without_date if t.get("source") == "manual"]
        other_without = [t for t in without_date if t.get("source") != "manual"]
        return manual_with + other_with + manual_without + other_without
    return []


def tasks_list_filtered(tasks: list, filter_type: str) -> str:
    """
    Форматируем задания по фильтру, группируя по курсу.
    urgent — до 3 дней, soon — 3–7 дней, all — все с датой, no_date — без даты
    """
    filtered = _get_filtered_tasks(tasks, filter_type)
    pending_count = len([t for t in tasks if not t.get("done")])

    headers = {
        "urgent": "🔴 *Срочные — до 3 дней*",
        "all":    "🟢 *Все задания*",
    }
    empties = {
        "urgent": "🟢 Срочных заданий нет!",
        "all":    "📋 Заданий нет!",
    }

    if filter_type not in headers:
        return "❌ Неизвестный фильтр"

    header = headers[filter_type]
    empty = empties[filter_type]

    if not filtered:
        return empty

    # Итоговая строка
    total_pending = len([t for t in tasks if not t.get("done")])
    total_done = len([t for t in tasks if t.get("done")])

    # Группируем по курсу
    by_course: dict[str, list] = {}
    for t in filtered:
        course = _short_course(t.get("course_name", ""))
        by_course.setdefault(course, []).append(t)

    lines = [header, ""]

    for course, course_tasks in by_course.items():
        src = course_tasks[0].get("source", "manual")
        if src == "manual":
            lines.append(f"✍️ *Личные задачи*")
        else:
            src_emoji = SOURCE_EMOJI.get(src, "📌")
            lines.append(f"{src_emoji} *{course}*")

        for t in course_tasks:
            emoji = _deadline_emoji(t.get("deadline"))
            date = _format_date(t.get("deadline"))
            title = _s(t.get("title"))
            # Укорачиваем название если длинное
            if len(title) > 45:
                title = title[:43] + "…"
            if date:
                lines.append(f"  {emoji} {title} — _{date}_")
            else:
                lines.append(f"  📌 {title}")

        lines.append("")
    lines.append(f"_Показано: {len(filtered)} | Всего невыполнено: {total_pending} | Выполнено: {total_done}_")

    return "\n".join(lines)


# ─── Расписание ───────────────────────────────────────────────────────

def _split_into_pairs(lesson: dict) -> list:
    try:
        start_dt = datetime.datetime.fromisoformat(lesson["start"])
        end_dt = datetime.datetime.fromisoformat(lesson["end"])
    except Exception:
        return [lesson]

    start_t = start_dt.time().replace(second=0, microsecond=0)
    end_t = end_dt.time().replace(second=0, microsecond=0)

    def to_m(t): return t.hour * 60 + t.minute
    start_m, end_m = to_m(start_t), to_m(end_t)

    matched = []
    for slot_start, slot_end in PAIR_SLOTS:
        s, e = to_m(slot_start), to_m(slot_end)
        if s >= start_m - 5 and e <= end_m + 5:
            matched.append(slot_start)

    if len(matched) <= 1:
        return [lesson]

    result = []
    for slot_t in sorted(matched):
        copy = dict(lesson)
        copy["start_time"] = slot_t.strftime("%H:%M")
        result.append(copy)
    return result


def _lesson_emoji(lesson: dict) -> str:
    desc = _sl(lesson.get("description"))
    name = _sl(lesson.get("name")) + " " + _sl(lesson.get("course_name"))
    location = _sl(lesson.get("location"))
    if "lxp" in location or "lxp" in name:
        return "🔵"
    if "лекц" in desc or "лекц" in name:
        return "🔷"
    if any(x in desc for x in ["практ", "семин", "лаб", "вебин", "test", "revision", "achievement"]):
        return "🔺"
    if any(x in name for x in ["практ", "семин", "лаб", "вебин", "мастерская"]):
        return "🔺"
    return "🔷"


def _lesson_suffix(lesson: dict) -> str:
    desc = _s(lesson.get("description"))
    location = _sl(lesson.get("location"))
    parts = []
    if desc:
        dl = desc.lower()
        if any(x in dl for x in ["лекц", "практ", "семин", "лаб", "вебин", "test", "revision", "achievement", "занятие"]):
            parts.append(desc)
    if "lxp" in location:
        parts.append("LXP")
    return ". ".join(parts) if parts else ""


def _expand_and_sort(lessons: list) -> list:
    expanded = []
    for lesson in lessons:
        expanded.extend(_split_into_pairs(lesson))
    expanded.sort(key=lambda x: _s(x.get("start_time")))
    return expanded


def format_schedule_by_day(schedule_by_day: dict, title: str) -> str:
    has_any = any(v for v in schedule_by_day.values())
    if not has_any:
        return f"📅 *{title}*\n\nПар нет 🎉"

    lines = [f"📅 *{title}*\n"]
    for date_str, lessons in sorted(schedule_by_day.items()):
        if not lessons:
            continue
        try:
            date = datetime.date.fromisoformat(date_str)
            day_name = DAY_NAMES[date.weekday()]
            header = f"📌 *{date.day} {MONTH_NAMES[date.month - 1]}, {day_name}:*"
        except Exception:
            continue
        lines.append(header)
        for lesson in _expand_and_sort(lessons):
            emoji = _lesson_emoji(lesson)
            start = _s(lesson.get("start_time"))
            name = _s(lesson.get("course_name")) or _s(lesson.get("name"))
            suffix = _lesson_suffix(lesson)
            if suffix:
                lines.append(f"{emoji} {start} — {name}. {suffix}")
            else:
                lines.append(f"{emoji} {start} — {name}")
        lines.append("")
    return "\n".join(lines)


def schedule_today(schedule: list) -> str:
    now = datetime.datetime.now(tz=UFA_TZ)
    day_name = DAY_NAMES[now.weekday()]
    title = f"{now.day} {MONTH_NAMES[now.month - 1]}, {day_name}"
    if not schedule:
        return f"📅 *{title}*\n\nПар сегодня нет 🎉"

    lines = [f"📅 *{title}*\n"]
    for lesson in _expand_and_sort(schedule):
        try:
            start_dt = datetime.datetime.fromisoformat(lesson["start"])
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=UFA_TZ)
            if start_dt < now:
                emoji = "✅"
            elif (start_dt - now).total_seconds() < 3600:
                emoji = "🔔"
            else:
                emoji = _lesson_emoji(lesson)
        except Exception:
            emoji = _lesson_emoji(lesson)

        start = _s(lesson.get("start_time"))
        name = _s(lesson.get("course_name")) or _s(lesson.get("name"))
        suffix = _lesson_suffix(lesson)
        if suffix:
            lines.append(f"{emoji} {start} — {name}. {suffix}")
        else:
            lines.append(f"{emoji} {start} — {name}")
    return "\n".join(lines)


def schedule_week(schedule_by_day: dict, next_week: bool = False) -> str:
    label = "следующую неделю" if next_week else "эту неделю"
    return format_schedule_by_day(schedule_by_day, f"Расписание на {label}")


def schedule_month(schedule_by_day: dict) -> str:
    now = datetime.datetime.now(tz=UFA_TZ)
    month_name = MONTH_NAMES[now.month - 1].capitalize()
    return format_schedule_by_day(schedule_by_day, f"Расписание — {month_name} {now.year}")


def format_deadline(deadline_str: str) -> str:
    try:
        dt = datetime.datetime.fromisoformat(deadline_str).astimezone(UFA_TZ)
        now = datetime.datetime.now(tz=UFA_TZ)
        today = now.date()
        deadline_date = dt.date()
        days = (deadline_date - today).days
        date_str = dt.strftime("%d.%m.%Y %H:%M")
        if days < 0:
            urgency = "🔴 просрочено"
        elif days == 0:
            urgency = "🔴 сегодня"
        elif days == 1:
            urgency = "🟠 завтра"
        elif days <= 3:
            urgency = f"🟠 {days} дн."
        elif days <= 7:
            urgency = f"🟡 {days} дн."
        else:
            urgency = f"🟢 {days} дн."
        return f"{date_str} ({urgency})"
    except Exception:
        return deadline_str


def source_emoji(source: str) -> str:
    return SOURCE_EMOJI.get(source, "📌")


def tasks_list(tasks: list) -> str:
    return tasks_list_filtered(tasks, "all")


def morning_briefing(schedule: list, tasks: list) -> str:
    now = datetime.datetime.now(tz=UFA_TZ)
    day_name = DAY_NAMES[now.weekday()]
    date_str = f"{now.day} {MONTH_NAMES[now.month - 1]}, {day_name}"
    lines = [f"☀️ *Доброе утро! {date_str}*\n"]

    if schedule:
        lines.append("📅 *Сегодня:*")
        for lesson in _expand_and_sort(schedule):
            emoji = _lesson_emoji(lesson)
            name = _s(lesson.get("course_name")) or _s(lesson.get("name"))
            lines.append(f"  {emoji} {lesson['start_time']} — {name}")
    else:
        lines.append("📅 Пар сегодня нет 🎉")

    lines.append("")
    pending = [t for t in tasks if not t.get("done")]
    urgent = [t for t in pending if t.get("deadline") and
              (datetime.datetime.fromisoformat(t["deadline"]).astimezone(UFA_TZ) - now).days <= 3]

    if urgent:
        lines.append(f"⚠️ *Срочных заданий: {len(urgent)}*")
        for t in urgent[:3]:
            date = _format_date(t.get("deadline"))
            lines.append(f"  🔴 {_short_course(t.get('course_name',''))} — {t['title'][:35]}")
        if len(urgent) > 3:
            lines.append(f"  _...и ещё {len(urgent) - 3}_")
    elif pending:
        lines.append(f"📚 Заданий: {len(pending)}, срочных нет ✅")
    else:
        lines.append("✅ Все задания выполнены!")

    return "\n".join(lines)


def evening_reminder(tasks: list) -> str:
    tomorrow = (datetime.datetime.now(tz=UFA_TZ) + datetime.timedelta(days=1)).date()
    lines = ["🌙 *Вечернее напоминание*\n"]
    due_tomorrow = [
        t for t in tasks
        if not t.get("done") and _get_deadline_date(t.get("deadline", "")) == tomorrow
    ]
    if due_tomorrow:
        lines.append("⚠️ *Завтра дедлайн:*")
        for task in due_tomorrow:
            lines.append(f"  🔴 {_short_course(task.get('course_name',''))} — {task['title'][:40]}")
    else:
        lines.append("✅ Завтра дедлайнов нет")
    return "\n".join(lines)


def deadline_reminder(task: dict, days_left: int) -> str:
    emoji = "🔴" if days_left <= 1 else "🟡" if days_left <= 3 else "🟢"
    deadline = format_deadline(task.get("deadline", ""))
    return (
        f"{emoji} *Напоминание о дедлайне*\n\n"
        f"📌 {task['title']}\n"
        f"📚 {_short_course(task.get('course_name', ''))}\n"
        f"⏰ {deadline}"
    )


def _esc(text: str) -> str:
    """Экранируем для HTML parse_mode."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def new_email_message(email_data: dict) -> str:
    """Красивое письмо — HTML формат."""
    sender = _esc(email_data.get("sender", ""))
    subject = _esc(email_data.get("subject", ""))
    date = _esc(email_data.get("date", ""))
    body = _esc(email_data.get("body", "")[:600])

    return (
        f"📧 <b>Яндекс Почта</b>\n"
        f"\n"
        f"📧 <b>Новое письмо</b>\n"
        f"{'─' * 20}\n"
        f"👤 <b>{sender}</b>\n"
        f"📋 {subject}\n"
        f"🕐 {date}\n"
        f"{'─' * 20}\n\n"
        f"{body}"
    )


def new_messenger_message(msg_data: dict) -> str:
    """Красивое сообщение из Яндекс Мессенджера — HTML формат."""
    sender = _esc(msg_data.get("sender", ""))
    date = _esc(msg_data.get("date", ""))
    content = msg_data.get("text") or msg_data.get("preview", "")
    content = _esc(content[:600])

    return (
        f"💬 <b>Яндекс Мессенджер</b>\n"
        f"\n"
        f"💬 <b>Новое сообщение</b>\n"
        f"{'─' * 20}\n"
        f"👤 <b>{sender}</b>\n"
        f"🕐 {date}\n"
        f"{'─' * 20}\n\n"
        f"{content}"
    )


def new_grade_message(grade: dict) -> str:
    """Уведомление о новой/изменённой оценке."""
    source_name = {"lms": "LMS ТюмГУ", "modeus": "Modeus"}.get(grade.get("source", ""), "Оценка")
    course = _short_course(grade.get("course_name", ""))
    title = grade.get("subject_name") or grade.get("title", "")
    g = grade.get("value") or grade.get("grade", "")
    old_g = grade.get("old_value") or grade.get("old_grade")

    try:
        g_num = float(str(g).replace(",", "."))
        mark_emoji = "🟢" if g_num >= 20 or g_num >= 4.5 else "🟡" if g_num >= 10 or g_num >= 3.5 else "🔴"
    except Exception:
        mark_emoji = "📝"

    source_emoji = "📊" if grade.get("source") == "modeus" else "🎓"
    if old_g:
        change = f"*{old_g}* → *{g}*"
        notif_type = f"{mark_emoji} Оценка изменена"
    else:
        change = f"*{g}*"
        notif_type = f"{mark_emoji} Новая оценка"

    lines = [
        f"{source_emoji} *{source_name}*",
        f"",
        f"{notif_type}",
        f"{'─' * 20}",
    ]
    lines.append(f"📚 {course}")
    if title:
        lines.append(f"📌 {title}")
    lines.append(f"🎯 {change}")
    if grade.get("updated_by") or grade.get("by"):
        lines.append(f"👤 _{grade.get('updated_by') or grade.get('by')}_")
    if grade.get("course_total"):
        lines.append(f"\n📊 Текущий итог: *{grade['course_total']}*")

    return "\n".join(lines)


def lesson_reminder(lesson: dict, minutes_before: int = 30) -> str:
    """Уведомление за N минут до пары."""
    name = _s(lesson.get("course_name")) or _s(lesson.get("name"))
    start = _s(lesson.get("start_time"))
    location = _s(lesson.get("location"))
    pair_numbers = {
        "08:30": 1, "10:15": 2, "12:05": 3, "14:05": 4,
        "15:55": 5, "17:40": 6, "19:20": 7, "21:00": 8,
    }
    pair_num = pair_numbers.get(start)
    num_str = f" ({pair_num}-я пара)" if pair_num else ""
    loc_str = f"\n📍 {location}" if location and "lxp" not in location.lower() else ""
    lxp_str = "\n🔵 _онлайн (LXP)_" if location and "lxp" in location.lower() else ""
    return (
        f"🔔 *Modeus*\n"
        f"\n"
        f"🔔 *Через {minutes_before} минут пара{num_str}*\n"
        f"{'─' * 20}\n"
        f"📖 {name}\n🕐 {start}{loc_str}{lxp_str}"
    )


def _get_deadline_date(deadline_str: str):
    try:
        return datetime.datetime.fromisoformat(deadline_str).astimezone(UFA_TZ).date()
    except Exception:
        return None


def format_subject_grades(data: dict) -> str:
    """Красивый компактный вывод оценок по предмету."""
    course = data.get("course_name", "")
    lessons = data.get("lessons", [])
    total = data.get("total", "")

    MONTH = ["янв","фев","мар","апр","май","июн","июл","авг","сен","окт","ноя","дек"]

    def fmt_date(dt_str):
        if not dt_str:
            return ""
        try:
            import datetime as _dt
            dt = _dt.datetime.fromisoformat(dt_str)
            return f"{dt.day} {MONTH[dt.month-1]}"
        except Exception:
            return dt_str[:10]

    # Пороги оценок
    is_matan = "матем" in course.lower() and "анализ" in course.lower()
    thresholds = {"3": 51 if is_matan else 61, "4": 76, "5": 91}

    # Статистика
    present = sum(1 for l in lessons if l.get("attendance") == "PRESENT")
    absent = sum(1 for l in lessons if l.get("attendance") == "ABSENT")
    total_lessons = present + absent

    lines = [f"📚 *{course}*"]

    # Посещаемость в одну строку
    if total_lessons:
        att_pct = int(present / total_lessons * 100)
        lines.append(f"👥 Посещаемость: {present}/{total_lessons} ({att_pct}%)\n")

    # Журнал — каждая запись компактно
    for lesson in lessons:
        date = fmt_date(lesson.get("date", ""))
        name = lesson.get("name", "")
        att = lesson.get("attendance", "")
        scores = lesson.get("scores", [])

        att_icon = {"PRESENT": "✅", "ABSENT": "❌", "LATE": "⏰"}.get(att, "⬜️")
        score_str = ""
        if scores:
            score_str = "  🎯 " + ", ".join(
                f"*{s['value']}*" + (f" _{s['type']}_" if s.get("type") else "")
                for s in scores
            )

        # Обрезаем тему до 30 символов
        name_short = name[:30] + "…" if len(name) > 30 else name

        line = f"{att_icon} *{date}*"
        if name_short:
            line += f" {name_short}"
        if score_str:
            line += score_str
        elif not att:
            line += " _не выставлено_"
        lines.append(line)

    lines.append("")

    # Итог и прогресс до оценки
    if total:
        try:
            total_num = float(str(total).replace(",", "."))
            lines.append(f"📊 *Текущий итог: {total}*")

            # Определяем текущую оценку
            if total_num >= thresholds["5"]:
                grade_str = "🟢 Отлично (5)"
                next_str = ""
            elif total_num >= thresholds["4"]:
                need = thresholds["5"] - total_num
                grade_str = "🔵 Хорошо (4)"
                next_str = f"До 5: нужно ещё *{need:.1f}* баллов"
            elif total_num >= thresholds["3"]:
                need = thresholds["4"] - total_num
                grade_str = "🟡 Удовл. (3)"
                next_str = f"До 4: нужно ещё *{need:.1f}* баллов"
            else:
                need = thresholds["3"] - total_num
                grade_str = "🔴 Не зачтено"
                next_str = f"До 3: нужно ещё *{need:.1f}* баллов"

            lines.append(f"🏆 {grade_str}")

            # Нужно набрать до каждой оценки
            needs = []
            for grade_label, threshold in [("3", thresholds["3"]), ("4", thresholds["4"]), ("5", thresholds["5"])]:
                diff = threshold - total_num
                if diff > 0:
                    needs.append(f"  • До оценки {grade_label}: *+{diff:.1f}* балл{'а' if diff < 5 else 'ов'}")
            if needs:
                lines.append(f"🎯 *Нужно набрать:*")
                lines.extend(needs)

        except Exception:
            lines.append(f"📊 *Текущий итог: {total}*")

    # Осталось встреч
    remaining = data.get("remaining_lessons")
    if remaining is not None:
        lines.append(f"📅 Осталось ~{remaining} встреч")

    return "\n".join(lines)


def format_grade_notification_new(grade: dict) -> str:
    """Красивое уведомление о новой оценке или посещаемости."""
    MONTH = ["января","февраля","марта","апреля","мая","июня",
             "июля","августа","сентября","октября","ноября","декабря"]

    def fmt_date(dt_str):
        if not dt_str:
            return ""
        try:
            import datetime as _dt
            dt = _dt.datetime.fromisoformat(dt_str)
            return f"{dt.day} {MONTH[dt.month-1]}"
        except Exception:
            return ""

    grade_type = grade.get("type", "")
    course = _short_course(grade.get("course", "") or grade.get("course_name", ""))
    subject = grade.get("subject", "") or grade.get("subject_name", "")
    value = grade.get("value", "")
    old_value = grade.get("old_value")
    attendance = grade.get("attendance", "")
    lesson_date = fmt_date(grade.get("lesson_date", ""))
    by = grade.get("by", "") or grade.get("updated_by", "")

    lines = []

    if grade_type == "lesson":
        att_str = ""
        if attendance == "PRESENT":
            att_str = "✅ *Присутствие* отмечено"
        elif attendance == "ABSENT":
            att_str = "❌ *Отсутствие* отмечено"

        mark_emoji = "🟢"

        lines.append(f"🎓 *Новая запись в журнале — Modeus*\n")
        lines.append(f"📚 *{course}*")
        if subject and subject != course:
            lines.append(f"📌 {subject}")
        if lesson_date:
            lines.append(f"📅 {lesson_date}")
        if att_str:
            lines.append(att_str)
        if value:
            change = f"*{old_value}* → *{value}*" if old_value else f"*{value}*"
            lines.append(f"{mark_emoji} Оценка: {change}")
        if by:
            lines.append(f"👤 _{by}_")
        if grade.get("course_total"):
            lines.append(f"\n📊 Текущий итог: *{grade['course_total']}*")

    elif grade_type == "current_total":
        lines.append(f"📊 *Итог обновлён — Modeus*\n")
        lines.append(f"📚 *{course}*")
        change = f"*{old_value}* → *{value}*" if old_value else f"*{value}*"
        lines.append(f"🎯 {change}")
        if by:
            lines.append(f"👤 _{by}_")

    elif grade_type == "module_total":
        lines.append(f"🏆 *Итог модуля — Modeus*\n")
        lines.append(f"📚 *{course}*")
        lines.append(f"🎯 *{value}*")

    return "\n".join(lines)


def format_lms_grade_notification(grade: dict) -> str:
    """Уведомление о новой оценке из LMS — формат как у Modeus."""
    course = _short_course(grade.get("course_name", ""))
    title = grade.get("title", "")
    value = grade.get("value", "")
    old_value = grade.get("old_value")
    by = grade.get("updated_by") or grade.get("by", "")

    if old_value:
        header = "🎓 *Оценка изменена* — LMS ТюмГУ"
        grade_line = f"🎯 *{old_value}* → *{value}*"
    else:
        header = "🎓 *Новая оценка* — LMS ТюмГУ"
        grade_line = f"🎯 *{value}*"

    lines = [f"{header}\n"]
    lines.append(f"📚 *{course}*")
    if title:
        lines.append(f"📌 {title}")
    lines.append(grade_line)
    if by:
        lines.append(f"👤 _{by}_")

    return "\n".join(lines)

