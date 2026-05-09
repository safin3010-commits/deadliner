import datetime
import re
from telegram import Update
from telegram.ext import (
    ContextTypes, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ConversationHandler
)
from config import MY_TELEGRAM_ID, UFA_TZ, USER_NAME
from storage import get_tasks, get_pending_tasks, mark_task_done, add_task
from bot.keyboards import (
    main_menu_keyboard, done_task_keyboard, tasks_filter_keyboard,
    tasks_filter_with_done_keyboard, schedule_period_keyboard,
    task_from_message_keyboard, delete_task_keyboard,
    edit_task_keyboard, edit_task_action_keyboard,
    grades_subjects_keyboard, grades_back_keyboard,
    reminder_task_keyboard, active_reminders_keyboard,
)
from bot.messages import (
    tasks_list_filtered, schedule_today, schedule_week, schedule_month
)

WAITING_TITLE = 1
WAITING_DEADLINE = 2
_pending_task = {}

MENU_BUTTONS = ["📋 Задания", "📅 Расписание", "🎓 Оценки"]


def is_authorized(user_id: int) -> bool:
    return user_id == MY_TELEGRAM_ID


# ─── Парсинг дат ──────────────────────────────────────────────────────

def _parse_dt(text: str) -> datetime.datetime | None:
    for fmt in ["%d.%m.%Y %H:%M", "%d.%m.%Y"]:
        try:
            return datetime.datetime.strptime(text.strip(), fmt).replace(tzinfo=UFA_TZ)
        except ValueError:
            continue
    return None


async def _parse_dt_smart(text: str) -> datetime.datetime | None:
    text = text.strip()
    for fmt in ["%d.%m.%Y %H:%M", "%d.%m.%Y"]:
        try:
            return datetime.datetime.strptime(text, fmt).replace(tzinfo=UFA_TZ)
        except ValueError:
            continue

    now = datetime.datetime.now(tz=UFA_TZ)
    tl = text.lower().strip()

    relative = {
        "сегодня": 0, "завтра": 1, "послезавтра": 2,
        "через день": 1, "через 2 дня": 2, "через 3 дня": 3,
        "через 4 дня": 4, "через 5 дней": 5, "через 6 дней": 6,
        "через неделю": 7, "через 2 недели": 14, "через месяц": 30,
    }
    if tl in relative:
        return (now + datetime.timedelta(days=relative[tl])).replace(hour=23, minute=59, second=0, microsecond=0)

    m = re.match(r'через\s+(\d+)\s+(день|дня|дней)', tl)
    if m:
        return (now + datetime.timedelta(days=int(m.group(1)))).replace(hour=23, minute=59, second=0, microsecond=0)

    m = re.match(r'через\s+(\d+)\s+(неделю|недели|недель)', tl)
    if m:
        return (now + datetime.timedelta(weeks=int(m.group(1)))).replace(hour=23, minute=59, second=0, microsecond=0)

    # Относительное время в минутах и часах — точное, без ИИ
    m = re.match(r'через\s+(\d+)\s+(минуту|минуты|минут|мин)', tl)
    if m:
        return now + datetime.timedelta(minutes=int(m.group(1)))

    m = re.match(r'через\s+(\d+)\s+(час|часа|часов)', tl)
    if m:
        return now + datetime.timedelta(hours=int(m.group(1)))

    m = re.match(r'через\s+(\d+)\s+(полчаса|полчасика)', tl)
    if m:
        return now + datetime.timedelta(minutes=30 * int(m.group(1)))

    # Время суток: "в HH:MM" или "в H:MM"
    m = re.match(r'в\s+(\d{1,2}):(\d{2})', tl)
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        candidate = now.replace(hour=h, minute=mn, second=0, microsecond=0)
        if candidate <= now:
            candidate += datetime.timedelta(days=1)
        return candidate

    # Просто "HH:MM" без "в"
    m = re.match(r'(\d{1,2}):(\d{2})$', tl)
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        candidate = now.replace(hour=h, minute=mn, second=0, microsecond=0)
        if candidate <= now:
            candidate += datetime.timedelta(days=1)
        return candidate

    months = {
        "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
        "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
    }
    m = re.match(r'(\d{1,2})\s+(' + '|'.join(months.keys()) + r')(?:\s+(\d{4}))?', tl)
    if m:
        day, month = int(m.group(1)), months[m.group(2)]
        year = int(m.group(3)) if m.group(3) else now.year
        try:
            dt = datetime.datetime(year, month, day, 23, 59, tzinfo=UFA_TZ)
            if dt < now and not m.group(3):
                dt = datetime.datetime(year + 1, month, day, 23, 59, tzinfo=UFA_TZ)
            return dt
        except ValueError:
            pass

    weekdays = {
        "понедельник": 0, "вторник": 1, "среда": 2, "среду": 2,
        "четверг": 3, "пятница": 4, "пятницу": 4,
        "суббота": 5, "субботу": 5, "воскресенье": 6,
    }
    for word, wd in weekdays.items():
        if word in tl:
            days_ahead = (wd - now.weekday()) % 7 or 7
            return (now + datetime.timedelta(days=days_ahead)).replace(hour=23, minute=59, second=0, microsecond=0)

    try:
        from grok import parse_date_with_groq
        date_str = await parse_date_with_groq(text)
        if date_str:
            return datetime.datetime.strptime(date_str, "%d.%m.%Y").replace(hour=23, minute=59, tzinfo=UFA_TZ)
    except Exception as e:
        print(f"Groq date parse error: {e}")

    return None


# ─── Основные команды ─────────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    await update.message.reply_text(
        "🖥 ИНИЦИАЛИЗАЦИЯ СИСТЕМЫ...\n\n"
        "▓▓▓▓▓▓▓▓▓▓ 100%\n\n"
        f"> ЖЕРТВА ИДЕНТИФИЦИРОВАНА: {USER_NAME}\n"
        "> СТАТУС: подозрительно бездельничает\n"
        "> ДЕДЛАЙНЫ: найдены\n"
        "> СОВЕСТЬ: не обнаружена\n\n"
        "⚠ СЛЕЖКА АКТИВИРОВАНА\n\n"
        "💡 _Просто напиши мне что нужно сделать — я пойму:_\n"
        "_«сдать лабу до пятницы», «напомни через 20 минут», «позвонить маме вечером»_",
        reply_markup=main_menu_keyboard()
    )


async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    text = update.message.text
    if text == "📋 Задания":
        await tasks_command(update, context)
    elif text == "📅 Расписание":
        await schedule_command(update, context)
    elif text == "🎓 Оценки":
        await grades_command(update, context)


async def tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    # Открываем сразу список "Все" без лишнего шага
    await show_tasks(update.message, "all")


async def show_tasks(message, filter_type: str):
    tasks = get_tasks()
    text = tasks_list_filtered(tasks, filter_type)
    keyboard = tasks_filter_with_done_keyboard(filter_type)
    if len(text) > 4000:
        parts = [text[i:i+4000] for i in range(0, len(text), 4000)]
        for i, part in enumerate(parts):
            kb = keyboard if i == len(parts) - 1 else None
            await message.reply_text(part, parse_mode="Markdown", reply_markup=kb)
    else:
        await message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def handle_tasks_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    filter_type = query.data.split(":")[1]
    await query.edit_message_reply_markup(reply_markup=None)
    await show_tasks(query.message, filter_type)


async def handle_done_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    back_filter = query.data.split(":")[1]
    from bot.messages import _get_filtered_tasks
    tasks = get_tasks()
    filtered = _get_filtered_tasks(tasks, back_filter)
    if not filtered:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("✅ В этом фильтре нет заданий!")
        return
    context.user_data["done_selected"] = []
    context.user_data["done_filter"] = back_filter
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        "☑️ *Выбери задания которые выполнил*\n_Можно выбрать несколько_",
        reply_markup=done_task_keyboard(filtered, back_filter=back_filter, selected=[]),
        parse_mode="Markdown"
    )


async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    await update.message.reply_text(
        "📅 *Выбери период:*",
        reply_markup=schedule_period_keyboard(),
        parse_mode="Markdown"
    )


def _merge_schedules(modeus: dict, netology: dict) -> dict:
    result = {}
    for key in set(modeus.keys()) | set(netology.keys()):
        result[key] = modeus.get(key, []) + netology.get(key, [])
        result[key].sort(key=lambda x: x.get("start_time", ""))
    return result


def _get_cache_info(week_start: datetime.date) -> str | None:
    try:
        from parsers.modeus import _load_schedule_cache
        cache = _load_schedule_cache()
        entry = cache.get(week_start.isoformat())
        if not entry:
            return None
        cached_at = datetime.datetime.fromisoformat(entry["cached_at"])
        age = datetime.datetime.now(tz=datetime.UTC) - cached_at
        hours = int(age.total_seconds() // 3600)
        minutes = int((age.total_seconds() % 3600) // 60)
        return f"{hours}ч {minutes}мин назад" if hours > 0 else f"{minutes}мин назад"
    except Exception:
        return None


async def handle_schedule_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    if data.startswith("sched_cache:") or data.startswith("sched_fresh:"):
        action, period = data.split(":", 1)
        use_cache = action == "sched_cache"
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("⏳ Загружаю расписание...")
        await _load_and_send_schedule(query.message, period, use_cache)
        return
    period = data.split(":")[1]
    await query.edit_message_reply_markup(reply_markup=None)
    if period == "month":
        await query.message.reply_text("⏳ Загружаю расписание...")
        await _load_and_send_schedule(query.message, period, use_cache=False)
        return
    if period == "today":
        import datetime as _dt
        from parsers.modeus import _load_schedule_cache
        from config import UFA_TZ as _TZ
        today = _dt.datetime.now(tz=_TZ).date()
        week_start_today = today - _dt.timedelta(days=today.weekday())
        cache = _load_schedule_cache()
        cache_entry = cache.get(week_start_today.isoformat())
        has_netology_cache = cache_entry and cache_entry.get("netology")
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        if has_netology_cache:
            cache_info = _get_cache_info(week_start_today)
            info_str = f" ({cache_info})" if cache_info else ""
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"📦 Из кэша{info_str}", callback_data="sched_cache:today")],
                [InlineKeyboardButton("🔄 Загрузить свежее", callback_data="sched_fresh:today")],
            ])
            await query.message.reply_text(
                f"📅 Есть сохранённое расписание{info_str}.\nЧто использовать?",
                reply_markup=keyboard
            )
        else:
            await query.message.reply_text("⏳ Загружаю расписание...")
            await _load_and_send_schedule(query.message, "today", use_cache=False)
        return
    from parsers.modeus import _get_week_start
    next_week = period == "week_next"
    week_start = _get_week_start(1 if next_week else 0)
    cache_info = _get_cache_info(week_start)
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    if cache_info:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"📦 Из кэша ({cache_info})", callback_data=f"sched_cache:{period}")],
            [InlineKeyboardButton("🔄 Загрузить свежее", callback_data=f"sched_fresh:{period}")],
        ])
        await query.message.reply_text(
            f"📅 Есть сохранённое расписание ({cache_info}).\nЧто использовать?",
            reply_markup=keyboard
        )
    else:
        await query.message.reply_text("⏳ Загружаю расписание...")
        await _load_and_send_schedule(query.message, period, use_cache=False)


async def _load_and_send_schedule(message, period: str, use_cache: bool):
    try:
        from parsers.modeus import (
            get_week_schedule, fetch_schedule_today, _get_week_start,
            _load_schedule_cache, _save_schedule_cache
        )
        from parsers.netology import fetch_netology_schedule_week
        import asyncio

        if period == "today":
            today = datetime.datetime.now(tz=UFA_TZ).date()
            week_start_today = today - datetime.timedelta(days=today.weekday())
            cache = _load_schedule_cache()
            cache_entry = cache.get(week_start_today.isoformat())
            netology_today = []

            if use_cache and cache_entry and cache_entry.get("netology"):
                netology_today = cache_entry["netology"].get(today.isoformat(), [])
                modeus_today = await fetch_schedule_today()
            else:
                if cache_entry and not use_cache:
                    cache_entry.pop("netology", None)
                    _save_schedule_cache(cache)
                modeus_today, netology_week = await asyncio.gather(
                    fetch_schedule_today(),
                    fetch_netology_schedule_week(week_start_today),
                    return_exceptions=True
                )
                if isinstance(netology_week, dict):
                    netology_today = netology_week.get(today.isoformat(), [])
                    if cache_entry:
                        cache_entry["netology"] = netology_week
                    else:
                        cache[week_start_today.isoformat()] = {"netology": netology_week}
                    _save_schedule_cache(cache)

            modeus_list = modeus_today if isinstance(modeus_today, list) else []
            combined = sorted(modeus_list + netology_today, key=lambda x: x.get("start_time", ""))
            text = schedule_today(combined)
        elif period in ("week_current", "week_next"):
            next_week = period == "week_next"
            week_start = _get_week_start(1 if next_week else 0)
            if use_cache:
                cache = _load_schedule_cache()
                entry = cache.get(week_start.isoformat())
                modeus_data = entry["data"] if entry else {}
                netology_data = {}
            else:
                cache = _load_schedule_cache()
                if week_start.isoformat() in cache:
                    del cache[week_start.isoformat()]
                    _save_schedule_cache(cache)
                modeus_data, netology_data = await asyncio.gather(
                    get_week_schedule(week_start),
                    fetch_netology_schedule_week(week_start),
                    return_exceptions=True
                )
                if isinstance(modeus_data, Exception):
                    modeus_data = {}
                if isinstance(netology_data, Exception):
                    netology_data = {}
            merged = _merge_schedules(
                modeus_data if isinstance(modeus_data, dict) else {},
                netology_data if isinstance(netology_data, dict) else {},
            )
            text = schedule_week(merged, next_week=next_week)
        elif period == "month":
            from parsers.modeus import get_cached_jwt, get_person_id_from_jwt, get_schedule
            import calendar as cal_mod
            jwt_token = await get_cached_jwt()
            person_id = get_person_id_from_jwt(jwt_token) if jwt_token else None
            if not person_id:
                await message.reply_text("❌ Не удалось получить расписание")
                return
            now = datetime.datetime.now(tz=UFA_TZ)
            last_day = cal_mod.monthrange(now.year, now.month)[1]
            schedule_by_day = {}
            for day_num in range(now.day, last_day + 1):
                day = datetime.date(now.year, now.month, day_num)
                schedule_by_day[day.isoformat()] = await get_schedule(jwt_token, person_id, day)
            text = schedule_month(schedule_by_day)
        else:
            text = "❌ Неизвестный период"

        if len(text) > 4000:
            for part in [text[i:i+4000] for i in range(0, len(text), 4000)]:
                await message.reply_text(part, parse_mode="Markdown")
        else:
            await message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        await message.reply_text(f"❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()


async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    msg = await update.message.reply_text("🔄 Синхронизирую задания...")
    try:
        from parsers.lms import fetch_lms_deadlines
        from parsers.netology import fetch_netology_deadlines
        from storage import save_tasks
        import asyncio

        tasks = get_tasks()
        existing_ids = {t.get("id") for t in tasks}
        existing_keys = {
            (t.get("title", ""), t.get("course_name", ""), t.get("deadline", ""))
            for t in tasks
        }
        added = 0

        lms_result, netology_result = await asyncio.gather(
            fetch_lms_deadlines(),
            fetch_netology_deadlines(),
            return_exceptions=True
        )

        lms_tasks = []
        if isinstance(lms_result, tuple):
            lms_tasks, completed_ids = lms_result
            for task in tasks:
                if task.get("source") == "lms" and task.get("id") in completed_ids:
                    if not task.get("done"):
                        task["done"] = True
        elif isinstance(lms_result, list):
            lms_tasks = lms_result

        netology_tasks = []
        if isinstance(netology_result, tuple):
            netology_tasks, _ = netology_result
        elif isinstance(netology_result, list):
            netology_tasks = netology_result

        updated = 0
        for t in lms_tasks + netology_tasks:
            task_id = t.get("id")
            found = False
            for existing in tasks:
                if str(existing.get("id")) == str(task_id):
                    found = True
                    if existing.get("deadline") != t.get("deadline") and t.get("deadline"):
                        existing["deadline"] = t["deadline"]
                        updated += 1
                    break
            if not found:
                key = (t.get("title", ""), t.get("course_name", ""))
                existing_key_pairs = {(e.get("title",""), e.get("course_name","")) for e in tasks}
                if key not in existing_key_pairs:
                    tasks.append(t)
                    existing_ids.add(task_id)
                    added += 1

        save_tasks(tasks)
        await msg.edit_text(
            f"✅ *Синхронизация завершена!*\n\nДобавлено: *{added}*\nОбновлено дедлайнов: *{updated}*\nВсего в базе: *{len(tasks)}*",
            parse_mode="Markdown"
        )
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка синхронизации: {e}")


# ─── Добавление задачи через меню ────────────────────────────────────

async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return ConversationHandler.END
    await update.message.reply_text(
        "✏️ Введи название задачи:\n_(или /cancel для отмены)_",
        parse_mode="Markdown"
    )
    return WAITING_TITLE


async def add_title_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text in MENU_BUTTONS:
        await menu_handler(update, context)
        return ConversationHandler.END
    _pending_task["title"] = update.message.text
    await update.message.reply_text(
        "📅 Введи дедлайн — любой формат:\n\n"
        "• `25.05.2025`\n"
        "• `6 апреля`\n"
        "• `завтра`, `через 3 дня`, `в пятницу`\n"
        "• или `без даты`",
        parse_mode="Markdown"
    )
    return WAITING_DEADLINE


async def add_deadline_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text in MENU_BUTTONS:
        await menu_handler(update, context)
        return ConversationHandler.END
    text = update.message.text.strip()
    title = _pending_task.get("title", "Задача")
    if text.lower() in ["без даты", "нет", "-", "no"]:
        task = add_task(title, None, "manual")
        await update.message.reply_text(
            f"✅ *Задача добавлена без даты!*\n\n📌 {task['title']}",
            parse_mode="Markdown"
        )
        return ConversationHandler.END
    dt = await _parse_dt_smart(text)
    if not dt:
        await update.message.reply_text(
            "❌ Не удалось распознать дату.\nПопробуй: `25.05.2025`, `завтра`, `6 апреля`, `через 3 дня`",
            parse_mode="Markdown"
        )
        return WAITING_DEADLINE
    task = add_task(title, dt.isoformat(), "manual")
    await update.message.reply_text(
        f"✅ *Задача добавлена!*\n\n📌 {task['title']}\n⏰ {dt.strftime('%d.%m.%Y %H:%M')}",
        parse_mode="Markdown"
    )
    return ConversationHandler.END


async def add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено")
    return ConversationHandler.END


# ─── Оценки ──────────────────────────────────────────────────────────

async def grades_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    msg = await update.message.reply_text("⏳ Загружаю предметы...")
    try:
        from parsers.modeus_grades import fetch_all_subjects
        subjects = await fetch_all_subjects()
        if not subjects:
            await msg.edit_text("❌ Не удалось загрузить предметы")
            return
        context.user_data["grades_subjects"] = subjects
        lines = ["🎓 *Оценки по предметам*\n_Выбери предмет:_\n"]
        for s in subjects:
            total = f" — *{s['total']}*" if s.get("total") else ""
            lines.append(f"• {s['name']}{total}")
        await msg.edit_text(
            "\n".join(lines),
            reply_markup=grades_subjects_keyboard(subjects),
            parse_mode="Markdown"
        )

    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")


# ─── Напоминания ─────────────────────────────────────────────────────

async def remind_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    from reminders import get_all_reminders
    tasks = get_pending_tasks()
    active = get_all_reminders()

    lines = ["🔔 *Напоминания*\n"]
    if active:
        from reminders import format_interval
        lines.append("*Активные:*")
        for r in active:
            lines.append(f"  • {r['task_title'][:30]} — {format_interval(r['interval_minutes'])}, осталось ×{r['times_left']}")
        lines.append("")

    lines.append("Выбери задачу чтобы добавить напоминание:")

    if not tasks:
        await update.message.reply_text("📋 Нет активных задач", parse_mode="Markdown")
        return

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    kb = reminder_task_keyboard(tasks)
    if active:
        kb_data = list(kb.inline_keyboard)
        kb_data.insert(0, [InlineKeyboardButton("📋 Управлять активными", callback_data="remind_list")])
        kb = InlineKeyboardMarkup(kb_data)

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=kb,
        parse_mode="Markdown"
    )


# ─── Свободный текст → задача/напоминание ─────────────────────────────

async def _try_parse_task_as_task(message, context, text: str):
    """Принудительно создаём задачу из текста (после уточнения)."""
    now = datetime.datetime.now(tz=UFA_TZ)
    # Простой парсинг времени из текста для дедлайна
    dt = await _parse_dt_smart(text)
    # Чистим текст от временных выражений для названия
    title = re.sub(r'через\s+\d+\s+\w+', '', text).strip()
    title = re.sub(r'(сегодня|завтра|послезавтра)', '', title).strip()
    if not title:
        title = text.strip()
    task = add_task(title, dt.isoformat() if dt else None, "manual")
    line = f"📌 {task['title']}"
    if dt:
        line += f" — {dt.strftime('%d.%m.%Y')}"
    await message.reply_text(f"✅ *Задача добавлена!*\n\n{line}", parse_mode="Markdown")


async def _try_parse_as_reminder_only(message, context, text: str):
    """Устанавливаем чистое напоминание (после уточнения)."""
    import re as _re3
    now = datetime.datetime.now(tz=UFA_TZ)
    tl = text.lower().strip()

    # Сначала локальный парсинг — надёжнее чем ИИ для относительного времени
    rem_dt = None
    _m = _re3.search(r'через\s+(\d+)\s+(минуту|минуты|минут|мин)', tl)
    if _m:
        rem_dt = now + datetime.timedelta(minutes=int(_m.group(1)))
    if not rem_dt:
        _m = _re3.search(r'через\s+(\d+)\s+(час|часа|часов)', tl)
        if _m:
            rem_dt = now + datetime.timedelta(hours=int(_m.group(1)))
    if not rem_dt:
        _m = _re3.search(r'в\s+(\d{1,2}):(\d{2})', tl)
        if _m:
            h, mn = int(_m.group(1)), int(_m.group(2))
            rem_dt = now.replace(hour=h, minute=mn, second=0, microsecond=0)
            if rem_dt <= now:
                rem_dt += datetime.timedelta(days=1)
    if not rem_dt:
        rem_dt = await _parse_dt_smart(text)

    if rem_dt:
        # Чистим текст от временных выражений
        rem_text = _re3.sub(r'(напомни|через\s+\d+\s+\w+|в\s+\d{1,2}:\d{2}|нужно|надо)', '', text, flags=_re3.IGNORECASE).strip()
        if not rem_text:
            rem_text = text
        rem_text = rem_text[0].upper() + rem_text[1:] if rem_text else text
        task = add_task(rem_text, rem_dt.isoformat(), "reminder_only")
        delay_mins = max(1, int((rem_dt - now).total_seconds() / 60))
        from reminders import add_reminder
        add_reminder(str(task["id"]), rem_text, delay_mins, 1, start_at=rem_dt.isoformat())
        time_fmt = rem_dt.strftime("%H:%M") if rem_dt.date() == now.date() else rem_dt.strftime("%d.%m %H:%M")
        await message.reply_text(f"🔔 *Напомню в {time_fmt}*\n\n_{rem_text}_", parse_mode="Markdown")
        return

    # Фоллбэк на ИИ — для сложных случаев типа "завтра утром"
    from grok import ask_grok
    import json as _json
    now_str = now.strftime('%d.%m.%Y %H:%M')
    prompt = (
        f"Текст: \"{text}\"\n"
        f"Сейчас: {now_str}\n\n"
        f"Определи время напоминания и текст. Верни JSON:\n"
        f"{{\"offset_minutes\": число_минут_от_сейчас_или_null, \"reminder_time\": \"DD.MM.YYYY HH:MM или null\", \"reminder_text\": \"...\"}}"
    )
    try:
        result = await ask_grok(prompt, system="Отвечай только JSON. Для относительного времени используй offset_minutes.")
        result = re.sub(r'```[a-z]*\n?', '', result).strip()
        data = _json.loads(re.search(r'\{.*\}', result, re.DOTALL).group())
        rem_text = data.get("reminder_text", text)
        _om = data.get("offset_minutes")
        if _om:
            rem_dt = now + datetime.timedelta(minutes=int(_om))
        else:
            rem_time_str = data.get("reminder_time", "")
            rem_dt = datetime.datetime.strptime(rem_time_str, "%d.%m.%Y %H:%M").replace(tzinfo=UFA_TZ)
        task = add_task(rem_text, rem_dt.isoformat(), "reminder_only")
        delay_mins = max(1, int((rem_dt - now).total_seconds() / 60))
        from reminders import add_reminder
        add_reminder(str(task["id"]), rem_text, delay_mins, 1, start_at=rem_dt.isoformat())
        time_fmt = rem_dt.strftime("%H:%M") if rem_dt.date() == now.date() else rem_dt.strftime("%d.%m %H:%M")
        await message.reply_text(f"🔔 *Напомню в {time_fmt}*\n\n_{rem_text}_", parse_mode="Markdown")
    except Exception as e:
        await message.reply_text(f"❌ Не удалось установить напоминание: {e}")


async def _parse_reminder_time_local(text: str, now: datetime.datetime) -> tuple:
    """
    Локальный парсинг времени напоминания без ИИ.
    Возвращает (rem_dt, clean_text) или (None, text).
    Ищет паттерны времени в любом месте строки.
    """
    import re as _re
    tl = text.lower().strip()
    rem_dt = None

    # Паттерны с минутами — ищем везде в строке
    m = _re.search(r'через\s+(\d+)\s+(минуту|минуты|минут|мин)', tl)
    if m:
        rem_dt = now + datetime.timedelta(minutes=int(m.group(1)))
        clean = _re.sub(r'через\s+\d+\s+(?:минуту|минуты|минут|мин)', '', text, flags=_re.IGNORECASE).strip()
        return rem_dt, clean

    # Паттерны с часами
    m = _re.search(r'через\s+(\d+)\s+(час|часа|часов)', tl)
    if m:
        rem_dt = now + datetime.timedelta(hours=int(m.group(1)))
        clean = _re.sub(r'через\s+\d+\s+(?:час|часа|часов)', '', text, flags=_re.IGNORECASE).strip()
        return rem_dt, clean

    # "в HH:MM" — ищем везде
    m = _re.search(r'в\s+(\d{1,2}):(\d{2})', tl)
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mn <= 59:
            rem_dt = now.replace(hour=h, minute=mn, second=0, microsecond=0)
            if rem_dt <= now:
                rem_dt += datetime.timedelta(days=1)
            clean = _re.sub(r'в\s+\d{1,2}:\d{2}', '', text, flags=_re.IGNORECASE).strip()
            return rem_dt, clean

    # Просто "HH:MM" в конце
    m = _re.search(r'(\d{1,2}):(\d{2})', tl)
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mn <= 59:
            rem_dt = now.replace(hour=h, minute=mn, second=0, microsecond=0)
            if rem_dt <= now:
                rem_dt += datetime.timedelta(days=1)
            clean = _re.sub(r'\d{1,2}:\d{2}', '', text, flags=_re.IGNORECASE).strip()
            return rem_dt, clean

    return None, text


def _clean_reminder_text(text: str) -> str:
    """
    Извлекаем смысловую часть из текста напоминания.
    Стратегия: убираем служебные блоки целиком (не по словам),
    потом чистим артефакты.
    """
    import re as _re
    t = text.strip()

    # Убираем целые служебные блоки в любом месте строки
    # Порядок важен: сначала длинные паттерны, потом короткие
    patterns = [
        r'хочу чтобы ты напомнил мне\s*',
        r'хочу чтобы ты\s*',
        r'чтобы ты\s*',
        r'напомни(л|ть)?\s+мне\s*',
        r'(напомни|напоминай|remind)\s*',
        r'через\s+\d+\s+(?:минуту|минуты|минут|мин|час|часа|часов)\s*',
        r'начиная\s+с\s+\d+\s+(?:утра|дня|вечера|ночи|часов|:00)\s*',
        r'начиная\s+с\s+\d{1,2}:\d{2}\s*',
        r'начиная\s+с\s*',
        r'каждые?\s+\d+\s+(?:минуту|минуты|минут|мин|час|часа|часов)\s*',
        r'каждый\s+час\s*',
        r'каждую\s+минуту\s*',
        r'\d+\s*раз[а]?\s*',
        r'в\s+\d{1,2}:\d{2}\s*',
        r'\b(нужно|надо|хочу|сделай|сделать|добав(ь)?|добавь|поставь|установи|создай|напоминание)\b\s*',
        r'\b(про|что|это|мне|ты|я|на завтра|на сегодня|на послезавтра)\b\s*',
        r'задач[ую]\s*',
    ]
    for p in patterns:
        t = _re.sub(p, ' ', t, flags=_re.IGNORECASE)

    # Убираем повисшие союзы и предлоги в начале
    t = _re.sub(r'^\s*[а-яёa-z]{1,3}\s+', '', t, flags=_re.IGNORECASE)
    # Убираем повторные пробелы и знаки препинания в начале/конце
    t = _re.sub(r'[,\.\s]+$', '', t)
    t = _re.sub(r'^[,\.\s]+', '', t)
    t = _re.sub(r'\s{2,}', ' ', t).strip()

    if not t or len(t) < 2:
        return text
    return t[0].upper() + t[1:]


def _is_clearly_reminder(text: str) -> bool:
    """True только если простое однократное напоминание — без ИИ."""
    import re as _re
    tl = text.lower()
    if _re.search(r'каждые?|каждую|каждый|каждое|\d+\s*раз[а]?|раз в|повтор|и потом|снова|опять', tl):
        return False
    has_rel = bool(_re.search(r'через\s+\d+\s+(?:минуту|минуты|минут|мин|час|часа|часов)', tl))
    has_abs = bool(_re.search(r'в\s+\d{1,2}:\d{2}', tl))
    if not (has_rel or has_abs):
        return False
    if _has_deadline_words(tl):
        return False
    return True
def _has_deadline_words(tl: str) -> bool:
    """Есть ли слова указывающие на дедлайн задачи."""
    import re as _re
    deadline_words = ['до ', 'дедлайн', 'сдать', 'сделать до', 'нужно до',
                      'пятниц', 'понедельник', 'вторник', 'среду', 'четверг',
                      'до завтра', 'до конца']
    return any(w in tl for w in deadline_words)


async def _try_parse_task_from_text(update, context, text: str):
    """
    Парсинг: ИИ извлекает суть и дату, всегда спрашиваем — задача или напоминание.
    """
    try:
        from grok import ask_grok
        import json as _json
        import re as _re
        import time as _time

        now = datetime.datetime.now(tz=UFA_TZ)
        now_str = now.strftime('%d.%m.%Y')
        now_time_str = now.strftime('%H:%M')
        weekday = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"][now.weekday()]
        day1 = (now + datetime.timedelta(days=1)).strftime('%d.%m.%Y')

        prompt = (
            "Текст: \"" + text + "\"\n"
            "Сейчас: " + now_str + " " + now_time_str + " (" + weekday + "). Завтра: " + day1 + "\n\n"
            "Извлеки все действия и даты. Верни JSON:\n"
            "{\"items\": [{\"action\": \"суть действия без дат и служебных слов\", \"deadline\": \"DD.MM.YYYY или null\"}]}\n\n"
            "ПРАВИЛА:\n"
            "- action: ТОЛЬКО действие. Забрать диск, Выпить воду, Позвонить маме\n"
            "- НЕ включай в action: даты, время, напомни, сдать, добавь\n"
            "- deadline: только явно указанная дата/день/относительная дата в формате DD.MM.YYYY\n"
            "- ВАЖНО: все даты должны быть в БУДУЩЕМ. Пятница = ближайшая будущая пятница\n"
            "- если несколько действий — несколько элементов в items\n\n"
            "ПРИМЕРЫ (сейчас 09.05.2026):\n"
            "забрать диск завтра → items=[{action:Забрать диск, deadline:" + day1 + "}]\n"
            "позвонить маме → items=[{action:Позвонить маме, deadline:null}]\n"
            "сдать лабу до пятницы → items=[{action:Сдать лабу, deadline:15.05.2026}]\n"
            "сдать до воскресенья → items=[{action:Сдать, deadline:10.05.2026}]\n"
            "Только JSON без пояснений."
        )

        result = await ask_grok(prompt, system="Ты анализатор текста. Отвечай только валидным JSON.")
        if not result:
            await update.message.reply_text("❌ Не удалось распознать запрос")
            return

        result = _re.sub(r'```[a-z]*\n?', '', result).strip()
        match = _re.search(r'\{.*\}', result, _re.DOTALL)
        if not match:
            await update.message.reply_text("❌ Не удалось распознать запрос")
            return
        try:
            data = _json.loads(match.group())
        except _json.JSONDecodeError as je:
            print(f"JSON parse error: {je}")
            await update.message.reply_text("❌ Не смог разобрать ответ ИИ. Попробуй переформулировать.")
            return

        items = data.get("items", [])
        if not items and data.get("action"):
            items = [{"action": data.get("action"), "deadline": data.get("deadline")}]
        if not items:
            await update.message.reply_text("❌ Не удалось распознать запрос. Попробуй переформулировать.")
            return

        async def _parse_deadline(deadline_str):
            if not deadline_str or deadline_str in ("null", "None", ""):
                return None
            now_local = datetime.datetime.now(tz=UFA_TZ)
            for fmt in ["%d.%m.%Y", "%d.%m.%Y %H:%M"]:
                try:
                    dt = datetime.datetime.strptime(deadline_str, fmt).replace(tzinfo=UFA_TZ)
                    # Если дата в прошлом — сдвигаем на неделю вперёд
                    if dt.date() < now_local.date():
                        dt += datetime.timedelta(weeks=1)
                    return dt
                except Exception:
                    continue
            dt = await _parse_dt_smart(deadline_str)
            if dt and dt.date() < now_local.date():
                dt += datetime.timedelta(weeks=1)
            return dt

        if len(items) > 1:
            # Проверяем: одно действие с разными датами = date_ambiguous
            actions = [i.get("action", "").strip().lower() for i in items if i.get("action")]
            all_same_action = len(set(actions)) == 1

            if all_same_action:
                # Одно действие, несколько дат — показываем выбор даты
                action = items[0].get("action", "").strip()
                if action:
                    action = action[0].upper() + action[1:]
                date_options = []
                for item in items:
                    dl_str = item.get("deadline")
                    if dl_str and dl_str not in ("null", "None", ""):
                        dl_dt = await _parse_deadline(dl_str)
                        if dl_dt:
                            date_options.append(dl_dt.strftime("%d.%m.%Y"))
                _ambig_key = f"ambig_{int(_time.time()*1000) & 0xFFFFFF}_{hash(text) & 0xFFFF}"
                context.user_data[_ambig_key] = {
                    "text": text,
                    "action": action,
                    "deadline_iso": None,
                }
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup as _IKM2
                kb = _IKM2([[
                    InlineKeyboardButton("📋 Задача", callback_data=f"type_task:{_ambig_key}"),
                    InlineKeyboardButton("🔔 Напоминание", callback_data=f"type_remind:{_ambig_key}"),
                ]])
                dates_str = " или ".join(date_options) if date_options else ""
                preview = f"_{action}_"
                if dates_str:
                    preview += f"\n📅 {dates_str}"
                await update.message.reply_text(
                    "🤔 *Что создать?*\n\n" + preview,
                    reply_markup=kb,
                    parse_mode="Markdown"
                )
                # Сохраняем date_options в wizard draft на случай напоминания
                context.user_data[_ambig_key]["date_options"] = date_options
                return
            else:
                # Разные действия — создаём все как задачи
                added = []
                for item in items:
                    action = (item.get("action") or "").strip()
                    if not action or action in ("null", "None"):
                        continue
                    action = action[0].upper() + action[1:]
                    deadline_dt = await _parse_deadline(item.get("deadline"))
                    task = add_task(action, deadline_dt.isoformat() if deadline_dt else None, "manual")
                    dl = f" — {deadline_dt.strftime('%d.%m.%Y')}" if deadline_dt else ""
                    added.append(f"📌 {task['title']}{dl}")
                if added:
                    await update.message.reply_text(
                        "✅ *Задачи добавлены:*\n\n" + "\n".join(added),
                        parse_mode="Markdown"
                    )
                return

        item = items[0]
        action = (item.get("action") or "").strip()
        if not action or action in ("null", "None"):
            action = _clean_reminder_text(text) or text[:50]
        if action:
            action = action[0].upper() + action[1:]

        deadline_dt = await _parse_deadline(item.get("deadline"))

        preview_lines = ["_" + action + "_"]
        if deadline_dt:
            preview_lines.append("📅 " + deadline_dt.strftime('%d.%m.%Y'))

        _ambig_key = f"ambig_{int(_time.time()*1000) & 0xFFFFFF}_{hash(text) & 0xFFFF}"
        context.user_data[_ambig_key] = {
            "text": text,
            "action": action,
            "deadline_iso": deadline_dt.isoformat() if deadline_dt else None,
        }

        from telegram import InlineKeyboardButton, InlineKeyboardMarkup as _IKM2
        kb = _IKM2([[
            InlineKeyboardButton("📋 Задача", callback_data=f"type_task:{_ambig_key}"),
            InlineKeyboardButton("🔔 Напоминание", callback_data=f"type_remind:{_ambig_key}"),
        ]])
        await update.message.reply_text(
            "🤔 *Что создать?*\n\n" + "\n".join(preview_lines),
            reply_markup=kb,
            parse_mode="Markdown"
        )

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"parse_task error: {e}")
        await update.message.reply_text("❌ Произошла ошибка при обработке запроса. Попробуй ещё раз.")

async def mode_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return

    text = update.message.text.strip()

    if text in MENU_BUTTONS:
        await menu_handler(update, context)
        return

    mode = context.user_data.get("_mode")

    if mode == "remind_interval":
        task_id = context.user_data.get("_remind_task_id")
        task_title = context.user_data.get("_remind_task_title", "Задача")
        context.user_data.pop("_mode", None)

        try:
            from grok import ask_grok
            import json as _json

            now = datetime.datetime.now(tz=UFA_TZ)
            now_str = now.strftime("%d.%m.%Y %H:%M")

            # Берём дедлайн задачи если есть
            tasks = get_tasks()
            task_obj = next((t for t in tasks if str(t["id"]) == str(task_id)), None)
            deadline_str = ""
            deadline_iso = ""
            days_to_deadline = 0
            if task_obj and task_obj.get("deadline"):
                try:
                    dl = datetime.datetime.fromisoformat(task_obj["deadline"]).astimezone(UFA_TZ)
                    deadline_str = dl.strftime("%d.%m.%Y %H:%M")
                    deadline_iso = dl.isoformat()
                    days_to_deadline = (dl - now).days
                except Exception:
                    pass

            deadline_hint = f"Дедлайн задачи: {deadline_str} (через {days_to_deadline} дн.)" if deadline_str else "Дедлайн не указан"

            prompt = (
                f"Текст пользователя: \"{text}\"\n"
                f"Сейчас: {now_str}\n"
                f"{deadline_hint}\n\n"
                f"Определи параметры напоминания и верни JSON:\n"
                f"{{\"interval\": минуты, \"times\": количество, \"start_at\": \"DD.MM.YYYY HH:MM или null\"}}\n\n"
                f"Правила:\n"
                f"1. 'каждый час 3 раза' → interval=60, times=3, start_at=null\n"
                f"2. 'каждые 30 минут 5 раз' → interval=30, times=5, start_at=null\n"
                f"3. 'за неделю до дедлайна раз в день' → interval=1440, times=7, start_at=дедлайн минус 7 дней\n"
                f"4. 'за неделю до дедлайна в 19:00' → interval=1440, times=7, start_at=дедлайн минус 7 дней в 19:00\n"
                f"5. 'каждый день начиная с пятницы' → interval=1440, times=7, start_at=ближайшая пятница\n"
                f"6. 'напомни 3 мая в 10:00' → interval=0, times=1, start_at=03.05.{now.year} 10:00\n"
                f"7. 'каждое утро в 9:00 до дедлайна' → interval=1440, times=дней_до_дедлайна, start_at=завтра 09:00\n"
                f"8. 'раз в 2 часа 10 раз с завтра' → interval=120, times=10, start_at=завтра 09:00\n"
                f"9. Если start_at=null — первое напоминание через interval минут от сейчас\n"
                f"10. ТОЛЬКО JSON без пояснений"
            )

            result = await ask_grok(prompt, system="Ты анализатор напоминаний. Отвечай только валидным JSON.")
            result = re.sub(r'```[a-z]*\n?', '', result).strip()
            result = re.sub(r'<think>.*?</think>', '', result, flags=re.DOTALL).strip()
            data = _json.loads(re.search(r'\{.*\}', result, re.DOTALL).group())

            interval = int(data.get("interval", 60))
            times = int(data.get("times", 3))
            start_at_str = data.get("start_at")

            # Парсим start_at
            start_at_iso = None
            if start_at_str and start_at_str not in ("null", "None", ""):
                try:
                    start_dt = datetime.datetime.strptime(start_at_str, "%d.%m.%Y %H:%M").replace(tzinfo=UFA_TZ)
                    start_at_iso = start_dt.isoformat()
                except Exception:
                    start_dt = await _parse_dt_smart(start_at_str)
                    if start_dt:
                        start_at_iso = start_dt.isoformat()

            from reminders import add_reminder, format_interval
            add_reminder(str(task_id), task_title, interval, times, start_at=start_at_iso)

            # Формируем подтверждение
            start_fmt = ""
            if start_at_iso:
                try:
                    s = datetime.datetime.fromisoformat(start_at_iso).astimezone(UFA_TZ)
                    start_fmt = f"\n📅 Начало: {s.strftime('%d.%m.%Y %H:%M')}"
                except Exception:
                    pass

            interval_str = format_interval(interval) if interval > 0 else "однократно"
            await update.message.reply_text(
                f"🔔 *Напоминание установлено!*\n\n"
                f"📌 {task_title}\n"
                f"⏱ {interval_str}, {times} раз{start_fmt}",
                parse_mode="Markdown"
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Не удалось распознать: {e}")
        return

    if mode == "rem_wiz_custom":
        field = context.user_data.pop("_rem_wiz_custom_field", None)
        context.user_data.pop("_mode", None)
        if field:
            await _handle_reminder_wizard_custom(update, context, field, text)
        return

    if not mode:
        await _try_parse_task_from_text(update, context, text)
        return

    # Остальные режимы (edit_title, edit_deadline, from_msg_*)
    await _handle_mode(update, context, mode, text)


async def _handle_mode(update, context, mode: str, text: str):
    """Обрабатываем активный режим редактирования."""
    from storage import save_tasks

    if mode == "edit_title":
        task_id = context.user_data.get("_edit_task_id")
        back_filter = context.user_data.get("_edit_back_filter", "all")
        context.user_data.pop("_mode", None)
        tasks = get_tasks()
        for t in tasks:
            if str(t["id"]) == str(task_id):
                t["title"] = text
                save_tasks(tasks)
                await update.message.reply_text(f"✅ *Название обновлено!*\n\n📌 {text}", parse_mode="Markdown")
                await show_tasks(update.message, back_filter)
                return
        await update.message.reply_text("❌ Задача не найдена")

    elif mode == "edit_deadline":
        task_id = context.user_data.get("_edit_task_id")
        back_filter = context.user_data.get("_edit_back_filter", "all")

        if text.lower() in ["без даты", "нет", "-"]:
            context.user_data.pop("_mode", None)
            tasks = get_tasks()
            for t in tasks:
                if str(t["id"]) == str(task_id):
                    t["deadline"] = None
                    save_tasks(tasks)
                    await update.message.reply_text("✅ *Дедлайн удалён*", parse_mode="Markdown")
                    await show_tasks(update.message, back_filter)
                    return
            await update.message.reply_text("❌ Задача не найдена")
            return

        dt = await _parse_dt_smart(text)
        if not dt:
            await update.message.reply_text(
                "❌ Не удалось распознать дату.\n`25.05.2025`, `завтра`, `6 апреля`",
                parse_mode="Markdown"
            )
            return

        context.user_data.pop("_mode", None)
        tasks = get_tasks()
        for t in tasks:
            if str(t["id"]) == str(task_id):
                t["deadline"] = dt.isoformat()
                save_tasks(tasks)
                await update.message.reply_text(
                    f"✅ *Дедлайн обновлён!*\n\n⏰ {dt.strftime('%d.%m.%Y %H:%M')}",
                    parse_mode="Markdown"
                )
                await show_tasks(update.message, back_filter)
                return
        await update.message.reply_text("❌ Задача не найдена")

    elif mode == "from_msg_title":
        context.user_data["_msg_task_title"] = text
        context.user_data["_mode"] = "from_msg_deadline"
        await update.message.reply_text(
            "📅 Введи дедлайн — любой формат:\n\n• `25.05.2025`\n• `завтра`\n• `без даты`",
            parse_mode="Markdown"
        )

    elif mode == "from_msg_deadline":
        title = context.user_data.get("_msg_task_title", "Задача")
        source = context.user_data.get("_msg_task_source", "manual")
        context.user_data.pop("_mode", None)

        if text.lower() in ["без даты", "нет", "-"]:
            task = add_task(title, None, source)
            await update.message.reply_text(
                f"✅ *Задача добавлена без даты!*\n\n📌 {task['title']}",
                parse_mode="Markdown"
            )
            return

        dt = await _parse_dt_smart(text)
        if not dt:
            await update.message.reply_text(
                "❌ Не удалось распознать дату.",
                parse_mode="Markdown"
            )
            context.user_data["_mode"] = "from_msg_deadline"
            return

        task = add_task(title, dt.isoformat(), source)
        await update.message.reply_text(
            f"✅ *Задача добавлена!*\n\n📌 {task['title']}\n⏰ {dt.strftime('%d.%m.%Y')}",
            parse_mode="Markdown"
        )


# ─── Callback кнопок ─────────────────────────────────────────────────

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("tasks:"):
        await handle_tasks_callback(update, context)

    elif data.startswith("done_pick:"):
        await handle_done_pick_callback(update, context)

    elif data.startswith("done_page:"):
        parts = data.split(":")
        back_filter = parts[1]
        page = int(parts[2]) if len(parts) > 2 else 0
        from bot.messages import _get_filtered_tasks
        filtered = _get_filtered_tasks(get_tasks(), back_filter)
        selected = context.user_data.get("done_selected", [])
        await query.edit_message_reply_markup(
            reply_markup=done_task_keyboard(filtered, back_filter=back_filter, page=page, selected=selected)
        )

    elif data.startswith("dtoggle:"):
        parts = data.split(":")
        tid, back_filter = parts[1], parts[2]
        page = int(parts[3]) if len(parts) > 3 else 0
        selected = context.user_data.get("done_selected", [])
        if tid in selected:
            selected.remove(tid)
        else:
            selected.append(tid)
        context.user_data["done_selected"] = selected
        from bot.messages import _get_filtered_tasks
        filtered = _get_filtered_tasks(get_tasks(), back_filter)
        await query.edit_message_reply_markup(
            reply_markup=done_task_keyboard(filtered, back_filter=back_filter, page=page, selected=selected)
        )

    elif data.startswith("done_save:"):
        back_filter = data.split(":")[1]
        selected = context.user_data.get("done_selected", [])
        if not selected:
            await query.answer("Ничего не выбрано!")
            return
        count = sum(1 for tid in selected if mark_task_done(tid))
        context.user_data["done_selected"] = []
        await query.edit_message_reply_markup(reply_markup=None)

        streak_msg = ""

        await query.message.reply_text(
            f"✅ Отмечено выполненными: *{count}*{streak_msg}",
            parse_mode="Markdown"
        )
        await show_tasks(query.message, back_filter)

    elif data.startswith("done:"):
        parts = data.split(":")
        raw_id, back_filter = parts[1], (parts[2] if len(parts) > 2 else "all")
        task_id = int(raw_id) if raw_id.isdigit() else raw_id
        success = mark_task_done(task_id)
        await query.edit_message_reply_markup(reply_markup=None)
        if success:
            await query.message.reply_text("✅ Выполнено!")
            await show_tasks(query.message, back_filter)
        else:
            await query.message.reply_text("❌ Задача не найдена")

    # ── Удаление ──────────────────────────────────────────────────────
    elif data.startswith("delete_pick:"):
        back_filter = data.split(":")[1]
        from bot.messages import _get_filtered_tasks
        filtered = _get_filtered_tasks(get_tasks(), back_filter)
        context.user_data["del_selected"] = []
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            "🗑 *Выбери задачи для удаления:*",
            reply_markup=delete_task_keyboard(filtered, back_filter=back_filter, selected=[]),
            parse_mode="Markdown"
        )

    elif data.startswith("del_toggle:"):
        parts = data.split(":")
        tid, back_filter = parts[1], parts[2]
        page = int(parts[3]) if len(parts) > 3 else 0
        selected = context.user_data.get("del_selected", [])
        if tid in selected:
            selected.remove(tid)
        else:
            selected.append(tid)
        context.user_data["del_selected"] = selected
        from bot.messages import _get_filtered_tasks
        filtered = _get_filtered_tasks(get_tasks(), back_filter)
        await query.edit_message_reply_markup(
            reply_markup=delete_task_keyboard(filtered, back_filter=back_filter, page=page, selected=selected)
        )

    elif data.startswith("del_page:"):
        parts = data.split(":")
        back_filter, page = parts[1], int(parts[2])
        from bot.messages import _get_filtered_tasks
        filtered = _get_filtered_tasks(get_tasks(), back_filter)
        selected = context.user_data.get("del_selected", [])
        await query.edit_message_reply_markup(
            reply_markup=delete_task_keyboard(filtered, back_filter=back_filter, page=page, selected=selected)
        )

    elif data.startswith("del_confirm:"):
        back_filter = data.split(":")[1]
        selected = context.user_data.get("del_selected", [])
        if not selected:
            await query.answer("Ничего не выбрано!")
            return
        from storage import save_tasks
        tasks = get_tasks()
        tasks = [t for t in tasks if str(t["id"]) not in selected]
        save_tasks(tasks)
        # Удаляем связанные напоминания
        try:
            from reminders import delete_reminder, _load, _save
            reminders = _load()
            reminders = [r for r in reminders if str(r.get("task_id", "")) not in selected]
            _save(reminders)
        except Exception as e:
            print(f"Reminder cleanup error: {e}")
        # Удаляем связанные reminder_only задачи
        try:
            from storage import save_tasks
            all_tasks = get_tasks()
            reminder_only_ids = {
                str(t["id"]) for t in all_tasks
                if t.get("source") == "reminder_only" and str(t["id"]) in selected
            }
            if reminder_only_ids:
                all_tasks = [t for t in all_tasks if str(t["id"]) not in reminder_only_ids]
                save_tasks(all_tasks)
        except Exception as e:
            print(f"Reminder_only cleanup error: {e}")
        count = len(selected)
        context.user_data["del_selected"] = []
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"🗑 Удалено: *{count}*", parse_mode="Markdown")
        await show_tasks(query.message, back_filter)

    # ── Редактирование ────────────────────────────────────────────────
    elif data.startswith("edit_pick:"):
        back_filter = data.split(":")[1]
        from bot.messages import _get_filtered_tasks
        filtered = _get_filtered_tasks(get_tasks(), back_filter)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            "✏️ *Выбери задачу для редактирования:*",
            reply_markup=edit_task_keyboard(filtered, back_filter=back_filter),
            parse_mode="Markdown"
        )

    elif data.startswith("edit_page:"):
        parts = data.split(":")
        back_filter, page = parts[1], int(parts[2])
        from bot.messages import _get_filtered_tasks
        filtered = _get_filtered_tasks(get_tasks(), back_filter)
        await query.edit_message_reply_markup(
            reply_markup=edit_task_keyboard(filtered, back_filter=back_filter, page=page)
        )

    elif data.startswith("edit_select:"):
        parts = data.split(":")
        task_id, back_filter = parts[1], parts[2]
        await query.edit_message_reply_markup(reply_markup=None)
        tasks = get_tasks()
        task = next((t for t in tasks if str(t["id"]) == task_id), None)
        if not task:
            await query.message.reply_text("❌ Задача не найдена")
            return
        await query.message.reply_text(
            f"✏️ *{task['title'][:50]}*\nЧто изменить?",
            reply_markup=edit_task_action_keyboard(task_id, back_filter),
            parse_mode="Markdown"
        )

    elif data.startswith("edit_title:"):
        parts = data.split(":")
        task_id, back_filter = parts[1], parts[2]
        context.user_data["_mode"] = "edit_title"
        context.user_data["_edit_task_id"] = task_id
        context.user_data["_edit_back_filter"] = back_filter
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("✏️ Введи новое название:\n_(или /cancel)_", parse_mode="Markdown")

    elif data.startswith("edit_deadline:"):
        parts = data.split(":")
        task_id, back_filter = parts[1], parts[2]
        tasks = get_tasks()
        task = next((t for t in tasks if str(t["id"]) == task_id), None)
        current = task.get("deadline", "не задан") if task else "не задан"
        context.user_data["_mode"] = "edit_deadline"
        context.user_data["_edit_task_id"] = task_id
        context.user_data["_edit_back_filter"] = back_filter
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            f"📅 Введи новый дедлайн:\n_Сейчас: {current}_\n\n`25.05.2025`, `завтра`, `без даты`",
            parse_mode="Markdown"
        )

    # ── Оценки ────────────────────────────────────────────────────────
    elif data.startswith("grades_subject:"):
        cur_id = data.split(":", 1)[1]
        await query.edit_message_reply_markup(reply_markup=None)
        msg = await query.message.reply_text("⏳ Загружаю оценки...")
        try:
            from parsers.modeus_grades import fetch_grades_for_subject
            from bot.messages import format_subject_grades
            data_grades = await fetch_grades_for_subject(cur_id)
            if not data_grades:
                await msg.edit_text("❌ Не удалось загрузить оценки")
                return
            text = format_subject_grades(data_grades)
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=grades_back_keyboard())
        except Exception as e:
            await msg.edit_text(f"❌ Ошибка: {e}")

    elif data == "grades_back":
        subjects = context.user_data.get("grades_subjects")
        await query.edit_message_reply_markup(reply_markup=None)
        if subjects:
            lines = ["🎓 *Оценки по предметам*\n_Выбери предмет:_\n"]
            for s in subjects:
                total = f" — *{s['total']}*" if s.get("total") else ""
                lines.append(f"• {s['name']}{total}")
            await query.message.reply_text(
                "\n".join(lines),
                reply_markup=grades_subjects_keyboard(subjects),
                parse_mode="Markdown"
            )
        else:
            await query.message.reply_text("Нажми 🎓 Оценки чтобы загрузить")

    # ── Напоминания ───────────────────────────────────────────────────
    elif data.startswith("rem_done:"):
        # Отметить задачу выполненной прямо из напоминания
        parts = data.split(":")
        task_id = parts[1] if len(parts) > 1 else ""
        rem_id = parts[2] if len(parts) > 2 else ""
        from reminders import delete_reminder
        from storage import save_tasks
        if rem_id:
            delete_reminder(rem_id)
        if task_id:
            tasks = get_tasks()
            task = next((t for t in tasks if str(t["id"]) == str(task_id)), None)
            if task and task.get("source") == "reminder_only":
                # reminder_only — просто удаляем, не копим в базе
                tasks = [t for t in tasks if str(t["id"]) != str(task_id)]
                save_tasks(tasks)
            else:
                mark_task_done(task_id)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("✅ Готово, напоминание удалено!")

    elif data.startswith("rem_delete:"):
        # Удалить напоминание полностью
        rem_id = data.split(":")[1]
        from reminders import delete_reminder
        delete_reminder(rem_id)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.answer("🗑 Напоминание удалено")

    elif data.startswith("rem_snooze:"):
        # Отложить напоминание на N минут
        parts = data.split(":")
        rem_id = parts[1]
        minutes = int(parts[2]) if len(parts) > 2 else 15
        from reminders import _load, _save
        import datetime as _dt
        from config import UFA_TZ as _TZ
        reminders = _load()
        for r in reminders:
            if r["id"] == rem_id:
                new_time = _dt.datetime.now(tz=_TZ) + _dt.timedelta(minutes=minutes)
                r["next_at"] = new_time.isoformat()
                r["times_left"] = max(r.get("times_left", 1), 1)
                break
        _save(reminders)
        import datetime as _dt2
        from config import UFA_TZ as _TZ2
        _snooze_time = (_dt2.datetime.now(tz=_TZ2) + _dt2.timedelta(minutes=minutes)).strftime("%H:%M")
        label = "15 мин" if minutes == 15 else "1 час"
        await query.edit_message_reply_markup(reply_markup=None)
        await query.answer(f"⏰ Напомню в {_snooze_time}")
        await query.message.reply_text(f"⏰ Напомню в *{_snooze_time}*", parse_mode="Markdown")

    elif data.startswith("remind_task:"):
        task_id = data.split(":")[1]
        tasks = get_tasks()
        task = next((t for t in tasks if str(t["id"]) == task_id), None)
        if not task:
            await query.message.reply_text("❌ Задача не найдена")
            return
        context.user_data["_mode"] = "remind_interval"
        context.user_data["_remind_task_id"] = task_id
        context.user_data["_remind_task_title"] = task.get("title", "Задача")
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            f"🔔 *{task['title'][:50]}*\n\n"
            f"Напиши когда и как часто напоминать:\n"
            f"_Например: 'каждый час 3 раза' или 'каждые 30 минут 5 раз'_",
            parse_mode="Markdown"
        )

    elif data.startswith("remind_page:"):
        page = int(data.split(":")[1])
        tasks = get_pending_tasks()
        await query.edit_message_reply_markup(
            reply_markup=reminder_task_keyboard(tasks, page=page)
        )

    elif data == "remind_list":
        from reminders import get_all_reminders
        active = get_all_reminders()
        if not active:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("Активных напоминаний нет")
            return
        await query.edit_message_reply_markup(
            reply_markup=active_reminders_keyboard(active)
        )

    elif data.startswith("remind_from_tasks:"):
        # Открываем сразу управление активными напоминаниями
        from reminders import get_all_reminders
        active = get_all_reminders()
        await query.edit_message_reply_markup(reply_markup=None)
        if not active:
            await query.message.reply_text("🔔 Активных напоминаний нет")
            return
        await query.message.reply_text(
            "🔔 *Активные напоминания*",
            reply_markup=active_reminders_keyboard(active),
            parse_mode="Markdown"
        )

    elif data.startswith("remind_postpone:"):
        # Перенести напоминание на +N минут
        parts = data.split(":")
        rem_id, add_mins = parts[1], int(parts[2])
        from reminders import _load, _save
        import datetime as _dtp
        from config import UFA_TZ as _TZP
        _rems = _load()
        for _r in _rems:
            if _r["id"] == rem_id:
                try:
                    _cur = _dtp.datetime.fromisoformat(_r["next_at"]).astimezone(_TZP)
                    # Если уже в прошлом — считаем от сейчас
                    _base = max(_cur, _dtp.datetime.now(tz=_TZP))
                    _r["next_at"] = (_base + _dtp.timedelta(minutes=add_mins)).isoformat()
                except Exception:
                    _r["next_at"] = (_dtp.datetime.now(tz=_TZP) + _dtp.timedelta(minutes=add_mins)).isoformat()
                break
        _save(_rems)
        from reminders import get_all_reminders
        active = get_all_reminders()
        _label = "1 ч" if add_mins == 60 else "3 ч"
        await query.answer(f"➕ Перенесено на {_label}")
        if active:
            await query.edit_message_reply_markup(reply_markup=active_reminders_keyboard(active))

    elif data.startswith("remind_info:"):
        # Тап на строку напоминания — ничего не делаем, просто отвечаем
        await query.answer()

    elif data.startswith("remind_del:"):
        reminder_id = data.split(":", 1)[1]
        from reminders import delete_reminder
        delete_reminder(reminder_id)
        from reminders import get_all_reminders
        active = get_all_reminders()
        await query.answer("🗑 Напоминание удалено")
        if active:
            await query.edit_message_reply_markup(reply_markup=active_reminders_keyboard(active))
        else:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("✅ Все напоминания удалены")

    # ── Общие ─────────────────────────────────────────────────────────
    elif data == "setup_warned":
        from main import _mark_setup_warned
        _mark_setup_warned()
        await query.edit_message_reply_markup(reply_markup=None)
        await query.answer("✅ Больше не будем напоминать")

    elif data == "cancel":
        context.user_data.pop("_mode", None)
        await query.edit_message_reply_markup(reply_markup=None)

    elif data.startswith("add_task:"):
        msg_text = query.message.text or ""
        source = "mail" if "📧" in msg_text else "messenger" if "💬" in msg_text else "manual"
        context.user_data["_mode"] = "from_msg_title"
        context.user_data["_msg_task_source"] = source
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("✏️ Введи название задачи:", parse_mode="Markdown")

    elif data.startswith("skip:"):
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("✅ Пропущено")

    elif data.startswith("ambig_task:") or data.startswith("ambig_remind:"):
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Напиши заново — я пойму 😊")

    elif data.startswith("type_task:"):
        _ambig_key = data.split(":", 1)[1]
        saved = context.user_data.pop(_ambig_key, None)
        await query.edit_message_reply_markup(reply_markup=None)
        if not saved:
            await query.message.reply_text("❌ Данные устарели, напиши заново")
            return
        action = saved.get("action", "Задача")
        deadline_iso = saved.get("deadline_iso")
        task = add_task(action, deadline_iso, "manual")
        dl = f" — {datetime.datetime.fromisoformat(deadline_iso).strftime("%d.%m.%Y")}" if deadline_iso else ""
        await query.message.reply_text(
            f"✅ *Задача добавлена!*\n\n📌 {task['title']}{dl}",
            parse_mode="Markdown"
        )

    elif data.startswith("type_remind:"):
        _ambig_key = data.split(":", 1)[1]
        saved = context.user_data.pop(_ambig_key, None)
        await query.edit_message_reply_markup(reply_markup=None)
        if not saved:
            await query.message.reply_text("❌ Данные устарели, напиши заново")
            return
        from bot.reminder_wizard import parse_reminder_intent, _set_draft, ask_next_question
        now = datetime.datetime.now(tz=UFA_TZ)
        orig_text = saved.get("text", "")
        action = saved.get("action", "")
        deadline_iso = saved.get("deadline_iso")
        intent = await parse_reminder_intent(orig_text, now)
        rem_text = (intent.get("reminder_text") or action or orig_text[:50]).strip()
        if rem_text:
            rem_text = rem_text[0].upper() + rem_text[1:]
        date_str = intent.get("date")
        if not date_str and deadline_iso:
            try:
                date_str = datetime.datetime.fromisoformat(deadline_iso).strftime("%d.%m.%Y")
            except Exception:
                pass
        # Локальный расчёт времени для "через X минут/часов"
        time_of_day = intent.get("time_of_day")
        time_known = intent.get("time_known", False)
        if not time_known:
            _m = re.search(r'через\s+(\d+)\s+(?:минуту|минуты|минут|мин)', orig_text.lower())
            if _m:
                _dt = now + datetime.timedelta(minutes=int(_m.group(1)))
                time_of_day = _dt.strftime("%H:%M")
                time_known = True
                if not date_str:
                    date_str = _dt.strftime("%d.%m.%Y")
            else:
                _m = re.search(r'через\s+(\d+)\s+(?:час|часа|часов)', orig_text.lower())
                if _m:
                    _dt = now + datetime.timedelta(hours=int(_m.group(1)))
                    time_of_day = _dt.strftime("%H:%M")
                    time_known = True
                    if not date_str:
                        date_str = _dt.strftime("%d.%m.%Y")

        draft = {
            "reminder_text": rem_text,
            "date": date_str,
            "date_ambiguous": intent.get("date_ambiguous", False),
            "date_options": intent.get("date_options", []),
            "time_of_day": time_of_day,
            "time_known": time_known,
            "interval_minutes": intent.get("interval_minutes"),
            "interval_known": intent.get("interval_known", False),
            "times_count": intent.get("times_count"),
            "times_known": intent.get("times_known", False),
            "is_recurring": intent.get("is_recurring", False),
        }
        _set_draft(context, draft)
        await ask_next_question(query.message, draft, context)

    elif data.startswith("rem_wiz:"):
        await _handle_reminder_wizard_callback(update, context, data)

    elif data.startswith("schedule:") or data.startswith("sched_cache:") or data.startswith("sched_fresh:"):
        await handle_schedule_callback(update, context)


# ─── Команды ─────────────────────────────────────────────────────────

async def itog_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    report = " ".join(context.args) if context.args else ""
    if not report:
        await update.message.reply_text(
            "📝 Напиши что сделал:\n_/itog сделал дискретку и матанализ_",
            parse_mode="Markdown"
        )
        return
    msg = await update.message.reply_text("🤖 Анализирую...")
    try:
        from grok import grok_evening_analysis
        from streak import mark_active_today, mark_evening_reported, streak_emoji
        streak_result = mark_active_today()
        mark_evening_reported()
        streak = streak_result["streak"]
        tasks = get_tasks()
        done_today = len([t for t in tasks if t.get("done")])
        pending = len([t for t in tasks if not t.get("done")])
        grok_text = await grok_evening_analysis(report, done_today, pending, streak)
        emoji = streak_emoji(streak)
        streak_line = ""
        if streak_result["is_new_record"]:
            streak_line = f"\n\n🏆 *Рекорд стрика: {streak} дн.!*"
        elif streak_result["continued"]:
            streak_line = f"\n\n{emoji} *Стрик: {streak} дн.*"
        elif streak == 1:
            streak_line = f"\n\n🌱 *Стрик: день 1!*"
        await msg.edit_text(f"📊 *Итог дня*\n\n{grok_text}{streak_line}", parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")


async def streak_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    try:
        from streak import streak_emoji, get_weekly_stats
        stats = get_weekly_stats()
        s, max_s = stats["streak"], stats["max_streak"]
        await update.message.reply_text(
            f"{streak_emoji(s)} *Стрик продуктивности*\n\n"
            f"Текущий: *{s} дн.*\nРекорд: *{max_s} дн.*\n\n"
            f"✅ Выполнено: *{stats['done_total']}*\n"
            f"📋 Осталось: *{stats['pending_total']}*",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def grades_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await grades_command(update, context)


# ─── Регистрация ─────────────────────────────────────────────────────


async def quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запускает мониторинг тестов LMS в фоне. На Windows/Linux принимает URL аргументом."""
    if not is_authorized(update.effective_user.id):
        return
    from storage import set_quiz_active
    set_quiz_active(True)
    import subprocess, sys, os

    # Если передан URL — сразу парсим без мониторинга (для Windows/Linux)
    args = context.args
    if args and args[0].startswith("http"):
        from quiz_monitor import process_quiz
        await update.message.reply_text("⏳ Парсю тест...")
        await process_quiz(args[0])
        return

    # macOS — автомониторинг Chrome
    if sys.platform != "darwin":
        await update.message.reply_text(
            "ℹ️ На Windows/Linux передай URL теста напрямую:\n"
            "`/quiz https://lms.utmn.ru/mod/quiz/attempt.php?attempt=...`",
            parse_mode="Markdown"
        )
        return

    # Проверяем не запущен ли уже
    import platform as _pl
    if _pl.system() == "Windows":
        await update.message.reply_text("ℹ️ На Windows автомониторинг недоступен.\nПередай URL теста: /quiz https://lms.utmn.ru/mod/quiz/attempt.php?attempt=...")
        return
    result = subprocess.run(["pgrep", "-f", "quiz_monitor.py"], capture_output=True, text=True)
    if result.stdout.strip():
        await update.message.reply_text(
            "🔍 Мониторинг уже запущен\n\n/quizstop — остановить",
            parse_mode="Markdown"
        )
        return

    venv_python = sys.executable
    proc = subprocess.Popen(
        [venv_python, "quiz_monitor.py"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    await update.message.reply_text(
        f"✅ *Мониторинг тестов запущен* (PID {proc.pid})\n\n"
        f"Открывай тест в Chrome — вопросы пришлю сюда\n\n"
        f"/quizstop — остановить",
        parse_mode="Markdown"
    )


async def quizstop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Останавливает мониторинг тестов."""
    if not is_authorized(update.effective_user.id):
        return
    from storage import set_quiz_active
    set_quiz_active(False)
    import subprocess, sys
    if sys.platform == "darwin":
        result = subprocess.run(["pkill", "-f", "quiz_monitor.py"], capture_output=True)
    else:
        result = subprocess.run(["taskkill", "/F", "/FI", "WINDOWTITLE eq quiz_monitor*"],
                                capture_output=True, shell=True)
    if result.returncode == 0:
        await update.message.reply_text("⏹ Мониторинг остановлен")
    else:
        await update.message.reply_text("ℹ️ Мониторинг не был запущен")


async def analysis_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Полный AI-анализ учёбы с разбивкой на 2 сообщения + Jarvis."""
    if not is_authorized(update.effective_user.id):
        return
    msg = await update.message.reply_text("📊 Собираю данные по всем предметам...")
    try:
        from parsers.study_analysis import fetch_study_analysis
        from grok import ask_grok
        import json as _json

        raw = await fetch_study_analysis()

        system = f"""Ты — академический аналитик успеваемости студента {USER_NAME} (1 курс ИСиТ, ТюмГУ+Нетология).
Делаешь отчёт для Telegram. Правила:
- *жирный* для важных цифр и названий
- Эмодзи для разделения секций
- Честно и конкретно, без воды
- Семестр ещё не закончен, баллы накапливаются
- Физра: 0 встреч — данные не поступают, не считай как прогулы
- Правоведение и Анализ данных — LXP, посещаемость не фиксируется
- История России = мастерская + лекции = итого 4.0 баллов
ВАЖНО: раздели ответ на ДВЕ части тегами [ЧАСТЬ1] и [ЧАСТЬ2].
Часть 1: анализ каждого предмета (баллы, шансы, посещаемость).
Часть 2: общий вывод, критические предметы, топ-3 приоритета, конкретный план."""

        now_str = datetime.datetime.now(tz=UFA_TZ).strftime("%d %B %Y")
        prompt = f"""Данные успеваемости на {now_str} (середина семестра):

{raw}

Сделай подробный аналитический отчёт. Раздели на [ЧАСТЬ1] и [ЧАСТЬ2]."""

        await msg.edit_text("🤖 Анализирую данные...")
        ai_text = await ask_grok(prompt, system=system, smart=False)

        if not ai_text:
            await msg.edit_text("❌ AI не ответил, попробуй позже")
            return

        # Разбиваем по тегам
        if "[ЧАСТЬ1]" in ai_text and "[ЧАСТЬ2]" in ai_text:
            parts = ai_text.split("[ЧАСТЬ2]")
            part1 = parts[0].replace("[ЧАСТЬ1]", "").strip()
            part2 = parts[1].strip()
        else:
            # Если теги не пришли — режем пополам по длине
            mid = len(ai_text) // 2
            # Ищем ближайший перенос строки к середине
            split_pos = ai_text.rfind("\n", 0, mid + 200)
            if split_pos == -1:
                split_pos = mid
            part1 = ai_text[:split_pos].strip()
            part2 = ai_text[split_pos:].strip()

        # Режем если всё равно слишком длинно
        if len(part1) > 3900:
            part1 = part1[:3900] + "..."
        if len(part2) > 3900:
            part2 = part2[:3900] + "..."

        await msg.edit_text(part1, parse_mode="Markdown")
        await update.message.reply_text(part2, parse_mode="Markdown")

        # Пишем в Jarvis — DeepSeek формулирует короткую озвучку на основе полного отчёта
        try:
            from scheduler import _jarvis_write
            from grok import ask_grok
            jarvis_prompt = (
                f"Вот полный анализ учёбы студента:\n{ai_text}\n\n"
                f"Сформулируй 2-3 предложения для голосового ассистента Джарвис. "
                f"Назови самые критичные предметы и главный совет. "
                f"Говори от третьего лица про студента {USER_NAME}. Без лишних слов."
            )
            jarvis_text = await ask_grok(jarvis_prompt, system="Ты голосовой ассистент Джарвис. Отвечай кратко, по делу, на русском.")
            if jarvis_text:
                _jarvis_write(f"📊 {jarvis_text}")
        except Exception:
            pass

    except Exception as e:
        import traceback
        await msg.edit_text(f"❌ Ошибка: {e}")
        traceback.print_exc()


async def say_command(update, context):
    """Озвучить текст на колонке. /say [комната] текст"""
    args = context.args
    if not args:
        await update.message.reply_text("Использование: /say текст\nИли: /say кухня текст\nКомнаты: гостиная, кухня, спальня")
        return

    from yandex_station import say_on_station, STATIONS

    if args[0].lower() in STATIONS:
        room = args[0].lower()
        text = " ".join(args[1:])
    else:
        room = "гостиная"
        text = " ".join(args)

    if not text:
        await update.message.reply_text("Укажи текст для озвучки.")
        return

    ok = await say_on_station(text, room)
    if ok:
        await update.message.reply_text(f"✅ Отправлено на колонку ({room}): {text}")
    else:
        await update.message.reply_text("❌ Не удалось отправить на колонку.")




# ─── Reminder Wizard callbacks ────────────────────────────────────────

async def _handle_reminder_wizard_callback(update, context, data: str):
    query = update.callback_query
    from bot.reminder_wizard import _get_draft, _set_draft, _clear_draft, ask_next_question, create_reminder_from_draft
    parts = data.split(":", 2)
    if len(parts) < 2:
        await query.answer("Ошибка")
        return
    _ = parts[0]
    field = parts[1] if len(parts) > 1 else ""
    value = parts[2] if len(parts) > 2 else ""
    draft = _get_draft(context)

    if field == "cancel":
        wiz_msgs = context.user_data.pop("_rem_wiz_msgs", [])
        for mid in wiz_msgs:
            try:
                await query.bot.delete_message(chat_id=query.message.chat_id, message_id=mid)
            except Exception:
                pass
        try:
            await query.message.delete()
        except Exception:
            pass
        _clear_draft(context)
        await query.message.reply_text("❌ Отменено")
        return

    if field == "confirm":
        wiz_msgs = context.user_data.pop("_rem_wiz_msgs", [])
        for mid in wiz_msgs:
            try:
                await query.bot.delete_message(chat_id=query.message.chat_id, message_id=mid)
            except Exception:
                pass
        try:
            await query.message.delete()
        except Exception:
            pass
        try:
            task, reminder, first_dt = await create_reminder_from_draft(draft)
            _clear_draft(context)
            now = datetime.datetime.now(tz=UFA_TZ)
            interval = draft.get("interval_minutes", 0)
            times = draft.get("times_count", 1)
            time_fmt = first_dt.strftime("%H:%M") if first_dt.date() == now.date() else first_dt.strftime("%d.%m в %H:%M")
            if interval and interval > 0:
                s = f"каждые {interval} мин" if interval < 60 else ("каждый час" if interval == 60 else f"каждые {interval//60} ч")
                repeat_str = f"{s}, {times} раз"
            else:
                repeat_str = "однократно"
            await query.message.reply_text(
                f"✅ *Напоминание создано!*\n\n📌 {draft.get('reminder_text')}\n🕐 Первое: *{time_fmt}*\n🔁 {repeat_str}",
                parse_mode="Markdown"
            )
        except Exception as e:
            _clear_draft(context)
            print(f"create_reminder_from_draft error: {e}")
            await query.message.reply_text(f"❌ Не удалось создать: {e}")
        return

    if field == "edit":
        _set_draft(context, {"reminder_text": draft.get("reminder_text", ""), "is_recurring": draft.get("is_recurring", False)})
        await query.edit_message_reply_markup(reply_markup=None)
        await ask_next_question(query.message, _get_draft(context), context)
        return

    if field == "date":
        if value == "custom":
            context.user_data["_mode"] = "rem_wiz_custom"
            context.user_data["_rem_wiz_custom_field"] = "date"
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("📅 Введи дату:\n_Например: 15 мая, 25.05.2026, через 3 дня_", parse_mode="Markdown")
            return
        draft["date"] = value
        draft["date_ambiguous"] = False
    elif field == "time":
        if value == "custom":
            context.user_data["_mode"] = "rem_wiz_custom"
            context.user_data["_rem_wiz_custom_field"] = "time"
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("🕐 Введи время:\n_Например: 10:30, в 9 утра_", parse_mode="Markdown")
            return
        draft["time_of_day"] = value
        draft["time_known"] = True
    elif field == "interval":
        if value == "custom":
            context.user_data["_mode"] = "rem_wiz_custom"
            context.user_data["_rem_wiz_custom_field"] = "interval"
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("⏱ Введи интервал:\n_Например: 45 минут, 2 часа, раз в день_", parse_mode="Markdown")
            return
        draft["interval_minutes"] = int(value)
        draft["interval_known"] = True
        if int(value) == 0:
            draft["times_count"] = 1
            draft["times_known"] = True
    elif field == "times":
        if value == "custom":
            context.user_data["_mode"] = "rem_wiz_custom"
            context.user_data["_rem_wiz_custom_field"] = "times"
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("🔁 Сколько раз напомнить?\n_Введи число_", parse_mode="Markdown")
            return
        draft["times_count"] = int(value)
        draft["times_known"] = True

    _set_draft(context, draft)
    await query.edit_message_reply_markup(reply_markup=None)
    await ask_next_question(query.message, draft, context)


async def _handle_reminder_wizard_custom(update, context, field: str, text: str):
    from bot.reminder_wizard import _get_draft, _set_draft, ask_next_question
    import re as _re
    draft = _get_draft(context)
    now = datetime.datetime.now(tz=UFA_TZ)
    try:
        if field == "date":
            dt = await _parse_dt_smart(text)
            if not dt:
                await update.message.reply_text("❌ Не распознал дату. Попробуй: _15 мая_, _25.05.2026_, _через 3 дня_", parse_mode="Markdown")
                context.user_data["_mode"] = "rem_wiz_custom"
                context.user_data["_rem_wiz_custom_field"] = "date"
                return
            draft["date"] = dt.strftime("%d.%m.%Y")
            draft["date_ambiguous"] = False
            if not (dt.hour == 23 and dt.minute == 59):
                draft["time_of_day"] = dt.strftime("%H:%M")
                draft["time_known"] = True
        elif field == "time":
            parsed_h, parsed_m = None, 0
            tl = text.lower().strip()
            m = _re.search(r'в?\s*(\d{1,2})\s*час', tl)
            if m: parsed_h = int(m.group(1))
            if parsed_h is None:
                m = _re.search(r'(\d{1,2})\s*(?:днём|дня|день)', tl)
                if m: parsed_h = int(m.group(1))
            if parsed_h is None:
                m = _re.search(r'(\d{1,2})\s*(?:вечером|вечера)', tl)
                if m:
                    h = int(m.group(1))
                    parsed_h = h + 12 if h < 12 else h
            if parsed_h is None:
                m = _re.search(r'(\d{1,2})\s*(?:утра|утром)', tl)
                if m:
                    h = int(m.group(1))
                    parsed_h = h if h >= 5 else h + 12
            if parsed_h is None:
                m = _re.search(r'(\d{1,2})\s*(?:ночи|ночью)', tl)
                if m:
                    h = int(m.group(1))
                    parsed_h = h if h < 5 else h
            if parsed_h is None:
                m = _re.search(r'(\d{1,2}):(\d{2})', text)
                if m: parsed_h, parsed_m = int(m.group(1)), int(m.group(2))
            if parsed_h is None:
                m = _re.search(r'^\s*(\d{1,2})\s*$', tl)
                if m: parsed_h = int(m.group(1))
            if parsed_h is not None and 0 <= parsed_h <= 23:
                draft["time_of_day"] = f"{parsed_h:02d}:{parsed_m:02d}"
                draft["time_known"] = True
            else:
                dt = await _parse_dt_smart(text)
                if dt and not (dt.hour == 23 and dt.minute == 59):
                    draft["time_of_day"] = dt.strftime("%H:%M")
                    draft["time_known"] = True
                else:
                    await update.message.reply_text(
                        "❌ Не распознал время. Попробуй: _15:00_, _в 9 утра_, _3 дня_, _вечером в 19_",
                        parse_mode="Markdown"
                    )
                    context.user_data["_mode"] = "rem_wiz_custom"
                    context.user_data["_rem_wiz_custom_field"] = "time"
                    return
        elif field == "interval":
            tl = text.lower()
            mins = None
            m = _re.search(r'(\d+)\s*(?:минут|мин)', tl)
            if m:
                mins = int(m.group(1))
            else:
                m = _re.search(r'(\d+)\s*(?:час|часа|часов)', tl)
                if m:
                    mins = int(m.group(1)) * 60
                else:
                    m = _re.search(r'(\d+)\s*(?:день|дня|дней|сутки)', tl)
                    if m:
                        mins = int(m.group(1)) * 1440
                    elif any(w in tl for w in ["раз в день","каждый день","ежедневно"]):
                        mins = 1440
                    elif any(w in tl for w in ["раз в час","каждый час"]):
                        mins = 60
            if mins is None:
                await update.message.reply_text("❌ Не распознал интервал. Попробуй: _45 минут_, _2 часа_, _раз в день_", parse_mode="Markdown")
                context.user_data["_mode"] = "rem_wiz_custom"
                context.user_data["_rem_wiz_custom_field"] = "interval"
                return
            draft["interval_minutes"] = mins
            draft["interval_known"] = True
        elif field == "times":
            m = _re.search(r'(\d+)', text)
            if m:
                draft["times_count"] = int(m.group(1))
                draft["times_known"] = True
            else:
                await update.message.reply_text("❌ Введи число. Например: _5_", parse_mode="Markdown")
                context.user_data["_mode"] = "rem_wiz_custom"
                context.user_data["_rem_wiz_custom_field"] = "times"
                return
        _set_draft(context, draft)
        await ask_next_question(update.message, draft, context)
    except Exception as e:
        print(f"_handle_reminder_wizard_custom error: {e}")
        import traceback; traceback.print_exc()
        await update.message.reply_text("❌ Ошибка. Попробуй ещё раз.")

def register_handlers(app):
    add_conv = ConversationHandler(
        entry_points=[
            CommandHandler("add", add_command),
            MessageHandler(filters.Regex("^➕ Добавить задачу$"), add_command),
        ],
        states={
            WAITING_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_title_received)],
            WAITING_DEADLINE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_deadline_received)],
        },
        fallbacks=[CommandHandler("cancel", add_cancel)],
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("tasks", tasks_command))
    app.add_handler(CommandHandler("schedule", schedule_command))
    app.add_handler(CommandHandler("cancel", add_cancel))
    app.add_handler(CommandHandler("itog", itog_command))
    app.add_handler(CommandHandler("streak", streak_command))
    app.add_handler(CommandHandler("grades", grades_cmd))
    app.add_handler(CommandHandler("quiz", quiz_command))
    app.add_handler(CommandHandler("analysis", analysis_command))
    app.add_handler(CommandHandler("quizstop", quizstop_command))

    app.add_handler(add_conv)
    app.add_handler(CallbackQueryHandler(button_callback))

    # Меню — ПЕРЕД mode_text_handler
    app.add_handler(MessageHandler(
        filters.Regex("^(📋 Задания|📅 Расписание|➕ Добавить задачу|🔄 Синхронизировать|🎓 Оценки|🔔 Напомнить)$"),
        menu_handler
    ))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mode_text_handler))
