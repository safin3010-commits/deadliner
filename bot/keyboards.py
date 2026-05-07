from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton("📋 Задания"), KeyboardButton("📅 Расписание")],
        [KeyboardButton("🎓 Оценки")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)


def tasks_filter_keyboard() -> InlineKeyboardMarkup:
    keyboard = [[
        InlineKeyboardButton("🔴 Срочные", callback_data="tasks:urgent"),
    ]]
    return InlineKeyboardMarkup(keyboard)


def tasks_filter_with_done_keyboard(filter_type: str) -> InlineKeyboardMarkup:
    from reminders import get_all_reminders
    has_reminders = bool(get_all_reminders())
    keyboard = [
        [InlineKeyboardButton("✅ Отметить выполненным", callback_data=f"done_pick:{filter_type}")],
        [
            InlineKeyboardButton("✏️ Редактировать", callback_data=f"edit_pick:{filter_type}"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"delete_pick:{filter_type}"),
        ],
        [InlineKeyboardButton("🔴 Срочные", callback_data="tasks:urgent")],
    ]
    if has_reminders:
        keyboard.append([InlineKeyboardButton("🔔 Управлять напоминаниями", callback_data=f"remind_from_tasks:{filter_type}")])
    return InlineKeyboardMarkup(keyboard)


def _group_tasks_by_course(tasks: list) -> list:
    """Группируем задачи по курсу сохраняя порядок курсов — как в списке заданий.
    Личные (manual) первыми, потом остальные по курсам."""
    seen_courses = []
    by_course = {}
    for t in tasks:
        from bot.messages import _short_course
        course_key = (t.get("source", ""), _short_course(t.get("course_name", "")))
        if course_key not in by_course:
            by_course[course_key] = []
            seen_courses.append(course_key)
        by_course[course_key].append(t)
    result = []
    for key in seen_courses:
        result.extend(by_course[key])
    return result


def done_task_keyboard(tasks: list, back_filter: str = "all", page: int = 0, selected: list = None) -> InlineKeyboardMarkup:
    selected = selected or []
    # Группируем по курсу как в списке заданий
    tasks = _group_tasks_by_course(tasks)
    page_size = 8
    start = page * page_size
    end = start + page_size
    page_tasks = tasks[start:end]
    keyboard = []
    for task in page_tasks:
        tid = str(task["id"])
        title = task.get("title", "")
        course = task.get("course_name", "")
        course_short = course[:12] + "…" if len(course) > 12 else course
        label = f"{course_short}: {title}" if course_short else title
        if len(label) > 36:
            label = label[:34] + "…"
        prefix = "☑️" if tid in selected else "⬜️"
        keyboard.append([InlineKeyboardButton(f"{prefix} {label}", callback_data=f"dtoggle:{tid}:{back_filter}:{page}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"done_page:{back_filter}:{page-1}"))
    if end < len(tasks):
        nav.append(InlineKeyboardButton(f"▶️ ({end}/{len(tasks)})", callback_data=f"done_page:{back_filter}:{page+1}"))
    if nav:
        keyboard.append(nav)
    if selected:
        keyboard.append([InlineKeyboardButton(f"💾 Сохранить ({len(selected)})", callback_data=f"done_save:{back_filter}")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data=f"tasks:{back_filter}")])
    return InlineKeyboardMarkup(keyboard)


def delete_task_keyboard(tasks: list, back_filter: str = "all", page: int = 0, selected: list = None) -> InlineKeyboardMarkup:
    selected = selected or []
    page_size = 8
    start = page * page_size
    end = start + page_size
    page_tasks = tasks[start:end]
    keyboard = []
    for task in page_tasks:
        tid = str(task["id"])
        title = task.get("title", "")[:36]
        prefix = "🗑" if tid in selected else "⬜️"
        keyboard.append([InlineKeyboardButton(f"{prefix} {title}", callback_data=f"del_toggle:{tid}:{back_filter}:{page}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"del_page:{back_filter}:{page-1}"))
    if end < len(tasks):
        nav.append(InlineKeyboardButton("▶️", callback_data=f"del_page:{back_filter}:{page+1}"))
    if nav:
        keyboard.append(nav)
    if selected:
        keyboard.append([InlineKeyboardButton(f"🗑 Удалить ({len(selected)})", callback_data=f"del_confirm:{back_filter}")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data=f"tasks:{back_filter}")])
    return InlineKeyboardMarkup(keyboard)


def edit_task_keyboard(tasks: list, back_filter: str = "all", page: int = 0) -> InlineKeyboardMarkup:
    tasks = _sort_tasks_by_deadline(tasks)
    page_size = 8
    start = page * page_size
    end = start + page_size
    page_tasks = tasks[start:end]
    keyboard = []
    for task in page_tasks:
        tid = str(task["id"])
        label = _fmt_task_label(task)
        keyboard.append([InlineKeyboardButton(f"✏️ {label}", callback_data=f"edit_select:{tid}:{back_filter}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"edit_page:{back_filter}:{page-1}"))
    if end < len(tasks):
        nav.append(InlineKeyboardButton("▶️", callback_data=f"edit_page:{back_filter}:{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data=f"tasks:{back_filter}")])
    return InlineKeyboardMarkup(keyboard)


def edit_task_action_keyboard(task_id: str, back_filter: str = "all") -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("✏️ Изменить название", callback_data=f"edit_title:{task_id}:{back_filter}")],
        [InlineKeyboardButton("📅 Изменить дедлайн", callback_data=f"edit_deadline:{task_id}:{back_filter}")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"tasks:{back_filter}")],
    ]
    return InlineKeyboardMarkup(keyboard)


def schedule_period_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("📅 Сегодня", callback_data="schedule:today")],
        [
            InlineKeyboardButton("📅 Эта неделя", callback_data="schedule:week_current"),
            InlineKeyboardButton("📅 Следующая неделя", callback_data="schedule:week_next"),
        ],
        [InlineKeyboardButton("📅 Месяц", callback_data="schedule:month")],
    ]
    return InlineKeyboardMarkup(keyboard)


def task_from_message_keyboard(message_id: str) -> InlineKeyboardMarkup:
    keyboard = [[
        InlineKeyboardButton("✅ Это домашнее задание", callback_data=f"add_task:{message_id}"),
        InlineKeyboardButton("❌ Пропустить", callback_data=f"skip:{message_id}"),
    ]]
    return InlineKeyboardMarkup(keyboard)


def grades_subjects_keyboard(subjects: list) -> InlineKeyboardMarkup:
    keyboard = []
    for s in subjects:
        keyboard.append([InlineKeyboardButton(s["name"], callback_data=f"grades_subject:{s['id']}")])
    return InlineKeyboardMarkup(keyboard)


def grades_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ К предметам", callback_data="grades_back")]])


def _fmt_task_label(task: dict, max_len: int = 45) -> str:
    """Форматируем кнопку: Предмет — Задача."""
    try:
        from bot.messages import _short_course, _deadline_emoji
    except Exception:
        _short_course = lambda x: x
        _deadline_emoji = lambda x: "📌"
    course = _short_course(task.get("course_name", ""))
    title = task.get("title", "")
    emoji = _deadline_emoji(task.get("deadline"))
    label = f"{course} — {title}" if course else title
    if len(label) > max_len:
        label = label[:max_len-1] + "…"
    return f"{emoji} {label}"


def _sort_tasks_by_deadline(tasks: list) -> list:
    """Сортируем задачи по дедлайну — срочные первыми."""
    import datetime
    from config import UFA_TZ
    now = datetime.datetime.now(tz=UFA_TZ)
    def key(t):
        d = t.get("deadline")
        if not d:
            return (1, "9999")
        return (0, d)
    manual = sorted([t for t in tasks if t.get("source") == "manual"], key=key)
    others = sorted([t for t in tasks if t.get("source") != "manual"], key=key)
    return manual + others


def reminder_task_keyboard(tasks: list, page: int = 0) -> InlineKeyboardMarkup:
    """Выбор задачи для напоминания — отсортировано по дедлайну."""
    tasks = _sort_tasks_by_deadline(tasks)
    page_size = 8
    start = page * page_size
    end = start + page_size
    page_tasks = tasks[start:end]
    keyboard = []
    for task in page_tasks:
        tid = str(task["id"])
        label = _fmt_task_label(task)
        keyboard.append([InlineKeyboardButton(f"🔔 {label}", callback_data=f"remind_task:{tid}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"remind_page:{page-1}"))
    if end < len(tasks):
        nav.append(InlineKeyboardButton("▶️", callback_data=f"remind_page:{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(keyboard)


def active_reminders_keyboard(reminders_list: list) -> InlineKeyboardMarkup:
    """Одна кнопка на напоминание: нажать = удалить."""
    import datetime
    from config import UFA_TZ
    now = datetime.datetime.now(tz=UFA_TZ)
    keyboard = []
    for r in reminders_list:
        title = r.get("task_title", "")[:22]
        times = r.get("times_left", 1)
        try:
            next_at = datetime.datetime.fromisoformat(r["next_at"]).astimezone(UFA_TZ)
            mins_left = int((next_at - now).total_seconds() / 60)
            if mins_left < 0:
                when = next_at.strftime("%H:%M")
            elif mins_left < 60:
                when = f"{next_at.strftime('%H:%M')} ({mins_left}м)"
            elif mins_left < 1440:
                when = f"{next_at.strftime('%H:%M')} ({mins_left//60}ч)"
            else:
                when = next_at.strftime("%d.%m %H:%M")
        except Exception:
            when = "—"
        repeat = f" ×{times}" if times > 1 else ""
        label = f"⏰ {title} — {when}{repeat}  🗑"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"remind_del:{r['id']}")])
    keyboard.append([InlineKeyboardButton("✖️ Закрыть", callback_data="cancel")])
    return InlineKeyboardMarkup(keyboard)
