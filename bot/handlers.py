import datetime
import re
from telegram import Update
from telegram.ext import (
    ContextTypes, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters,
)
from config import MY_TELEGRAM_ID, UFA_TZ, USER_NAME
from storage import get_tasks, get_pending_tasks, mark_task_done, add_task
from bot.keyboards import (
    main_menu_keyboard, done_task_keyboard,
    tasks_filter_with_done_keyboard, schedule_period_keyboard,
    delete_task_keyboard,
    edit_task_keyboard, edit_task_action_keyboard,
    grades_subjects_keyboard, grades_back_keyboard,
    reminder_task_keyboard, active_reminders_keyboard,
)
from bot.messages import (
    tasks_list_filtered, schedule_today, schedule_week, schedule_month, _esc_md
)

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
        print(f"Groq date parse error: {e!r}")

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
    # Modeus и Нетология — разные реальные источники пар (не дубли друг
    # друга, даже при совпадении времени/похожих названий курса) — просто
    # объединяем оба списка, без дедупликации между ними.
    result = {}
    for key in set(modeus.keys()) | set(netology.keys()):
        result[key] = sorted(modeus.get(key, []) + netology.get(key, []), key=lambda x: x.get("start_time", ""))
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


_MODEUS_AUTH_ERROR_TEXT = (
    "⚠️ Не удалось получить расписание Modeus — истёк логин/сессия или SSO не отвечает. "
    "Попробуй ещё раз через пару минут; если не поможет — проверь MODEUS_USERNAME/PASSWORD в .env."
)


async def _load_and_send_schedule(message, period: str, use_cache: bool):
    try:
        from parsers.modeus import (
            get_week_schedule, fetch_schedule_today, _get_week_start,
            _load_schedule_cache, _save_schedule_cache, ModeusAuthError,
            SCHEDULE_CACHE_FILE,
        )
        from parsers.netology import fetch_netology_schedule_week
        from data_lock import file_lock
        import asyncio

        if period == "today":
            today = datetime.datetime.now(tz=UFA_TZ).date()
            week_start_today = today - datetime.timedelta(days=today.weekday())
            cache = _load_schedule_cache()
            cache_entry = cache.get(week_start_today.isoformat())
            netology_today = []

            if use_cache and cache_entry and cache_entry.get("netology"):
                netology_today = cache_entry["netology"].get(today.isoformat(), [])
                try:
                    modeus_today = await fetch_schedule_today()
                except ModeusAuthError:
                    await message.reply_text(_MODEUS_AUTH_ERROR_TEXT)
                    return
            else:
                modeus_today, netology_week = await asyncio.gather(
                    fetch_schedule_today(),
                    fetch_netology_schedule_week(week_start_today),
                    return_exceptions=True
                )
                if isinstance(modeus_today, ModeusAuthError):
                    await message.reply_text(_MODEUS_AUTH_ERROR_TEXT)
                    return
                if isinstance(netology_week, dict):
                    netology_today = netology_week.get(today.isoformat(), [])
                    # Держим лок на весь читай-меняй-пиши, а не только на запись —
                    # иначе конкурентный refresh_schedule_cache_silent (раз в 4ч)
                    # мог затереть или потерять чужое обновление той же недели.
                    with file_lock(SCHEDULE_CACHE_FILE):
                        cache = _load_schedule_cache()
                        cache_entry = cache.get(week_start_today.isoformat())
                        if cache_entry:
                            cache_entry["netology"] = netology_week
                            cache_entry.setdefault("data", {})
                            cache_entry.setdefault("cached_at", datetime.datetime.now(tz=datetime.UTC).isoformat())
                        else:
                            # Всегда полная запись (data/cached_at/netology) — раньше
                            # здесь создавалась запись ТОЛЬКО с "netology", и любой
                            # код, читавший entry["data"] для той же недели, падал
                            # KeyError (например /schedule "эта неделя" → "из кэша").
                            cache[week_start_today.isoformat()] = {
                                "cached_at": datetime.datetime.now(tz=datetime.UTC).isoformat(),
                                "data": {},
                                "netology": netology_week,
                            }
                        _save_schedule_cache(cache)

            modeus_list = modeus_today if isinstance(modeus_today, list) else []
            # Modeus и Нетология — разные реальные пары, даже при совпадении
            # времени/похожих названий курса — не дедуплицировать между ними.
            combined = sorted(modeus_list + netology_today, key=lambda x: x.get("start_time", ""))
            text = schedule_today(combined)
        elif period in ("week_current", "week_next"):
            next_week = period == "week_next"
            week_start = _get_week_start(1 if next_week else 0)
            if use_cache:
                cache = _load_schedule_cache()
                entry = cache.get(week_start.isoformat())
                # .get(), не entry["data"] — запись недели может существовать
                # только с полем "netology" (см. запись ниже в ветке "today"),
                # без "data" — прямой доступ по ключу здесь падал KeyError.
                modeus_data = entry.get("data", {}) if entry else {}
                netology_data = entry.get("netology", {}) if entry else {}
            else:
                with file_lock(SCHEDULE_CACHE_FILE):
                    cache = _load_schedule_cache()
                    if week_start.isoformat() in cache:
                        del cache[week_start.isoformat()]
                        _save_schedule_cache(cache)
                modeus_data, netology_data = await asyncio.gather(
                    get_week_schedule(week_start),
                    fetch_netology_schedule_week(week_start),
                    return_exceptions=True
                )
                if isinstance(modeus_data, ModeusAuthError):
                    await message.reply_text(_MODEUS_AUTH_ERROR_TEXT)
                    return
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
            try:
                jwt_token = await get_cached_jwt()
            except ModeusAuthError:
                jwt_token = None
            person_id = get_person_id_from_jwt(jwt_token) if jwt_token else None
            if not person_id:
                await message.reply_text(_MODEUS_AUTH_ERROR_TEXT)
                return
            now = datetime.datetime.now(tz=UFA_TZ)
            last_day = cal_mod.monthrange(now.year, now.month)[1]
            days = [datetime.date(now.year, now.month, d) for d in range(now.day, last_day + 1)]

            # Все дни месяца — параллельно, а не по одному (было до 31
            # последовательных запросов, каждый с собственным 30с таймаутом).
            modeus_results = await asyncio.gather(
                *(get_schedule(jwt_token, person_id, day) for day in days),
                return_exceptions=True,
            )
            schedule_by_day = {
                day.isoformat(): (res if isinstance(res, list) else [])
                for day, res in zip(days, modeus_results)
            }

            # Раньше месяц показывал только пары Modeus — вебинары Нетологии
            # (schedule_cache.json хранит их отдельным списком "netology" на
            # каждую неделю) в месячный вид не попадали вообще.
            week_starts = sorted({d - datetime.timedelta(days=d.weekday()) for d in days})
            netology_results = await asyncio.gather(
                *(fetch_netology_schedule_week(ws) for ws in week_starts),
                return_exceptions=True,
            )
            for res in netology_results:
                if not isinstance(res, dict):
                    continue
                for date_iso, lessons in res.items():
                    if date_iso in schedule_by_day:
                        # Modeus (уже в schedule_by_day) и Нетология — разные реальные
                        # пары, не дедуплицировать.
                        schedule_by_day[date_iso] = schedule_by_day[date_iso] + lessons

            text = schedule_month(schedule_by_day)
        else:
            text = "❌ Неизвестный период"

        if len(text) > 4000:
            for part in [text[i:i+4000] for i in range(0, len(text), 4000)]:
                await message.reply_text(part, parse_mode="Markdown")
        else:
            await message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        await message.reply_text(f"❌ Ошибка: {e!r}")
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
            netology_tasks, _, netology_completed_ids = netology_result
            for task in tasks:
                if task.get("source") == "netology" and task.get("id") in netology_completed_ids:
                    if not task.get("done"):
                        task["done"] = True
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
        await msg.edit_text(f"❌ Ошибка синхронизации: {e!r}")


# ─── Добавление задачи через меню ────────────────────────────────────

async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Раньше здесь начинался двухшаговый диалог (название → отдельно дедлайн).
    Убрано вместе со всей старой цепочкой уточнений (см. smart_intent.py) —
    если после /add сразу идёт текст, обрабатываем его как обычное
    сообщение; если текста нет, просто напоминаем, что писать можно и без
    команды вообще."""
    if not is_authorized(update.effective_user.id):
        return
    text = " ".join(context.args) if context.args else ""
    if text:
        await handle_free_text(update, context, text)
    else:
        await update.message.reply_text(
            "Просто напиши обычным сообщением, что нужно сделать — "
            "я сам пойму, задача это, напоминание или вопрос."
        )


async def add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cancel — сбросить активный режим (редактирование задачи и т.п.),
    если он был; раньше это был fallback старого диалога /add, теперь общая
    команда сброса."""
    had_mode = context.user_data.pop("_mode", None) is not None
    await update.message.reply_text("❌ Отменено" if had_mode else "Нечего отменять")


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
        await msg.edit_text(f"❌ Ошибка: {e!r}")


# ─── Напоминания ─────────────────────────────────────────────────────

async def remind_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    from reminders import get_all_reminders
    tasks = get_pending_tasks()
    active = get_all_reminders()

    lines = ["🔔 *Напоминания*\n"]
    if active:
        from reminders import format_interval, format_times_left
        lines.append("*Активные:*")
        for r in active:
            lines.append(f"  • {_esc_md(r['task_title'][:30])} — {format_interval(r['interval_minutes'])}, осталось {format_times_left(r['times_left'])}")
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


# ─── Свободный текст → задача/напоминание/вопрос ──────────────────────
# Единая точка входа — см. bot/smart_intent.py. Раньше здесь была цепочка:
# обязательный вопрос "задача/напоминание/вопрос?" → слой regex-эвристик →
# Groq (ask_claude_fast) → часто ещё одно уточнение кнопкой → отдельный
# мастер параметров повтора. Один вызов Клода вместо всего этого.
from bot.smart_intent import handle_free_text

async def mode_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return

    text = update.message.text.strip()

    if text in MENU_BUTTONS:
        await menu_handler(update, context)
        return

    mode = context.user_data.get("_mode")

    if mode == "remind_interval":
        # Настройка напоминания для уже выбранной в меню задачи — тот же
        # надёжный путь (claude_session), что и весь остальной smart_intent,
        # раньше был отдельный самостоятельный вызов Groq.
        task_id = context.user_data.get("_remind_task_id")
        task_title = context.user_data.get("_remind_task_title", "Задача")
        context.user_data.pop("_mode", None)

        tasks = get_tasks()
        task_obj = next((t for t in tasks if str(t["id"]) == str(task_id)), None)
        deadline_iso = task_obj.get("deadline") if task_obj else None

        from bot.smart_intent import extract_reminder_params, _clamp_times, _parse_iso
        from reminders import format_interval, format_times_left, add_reminder
        data, err = await extract_reminder_params(task_title, deadline_iso, text)
        if err or not data:
            await update.message.reply_text(f"❌ Не удалось распознать{f': {err}' if err else ''}, попробуй переформулировать.")
            return

        try:
            interval = max(1, int(data.get("interval_minutes") or 60))
        except (TypeError, ValueError):
            interval = 60
        times = _clamp_times(data.get("times"))
        start_dt = _parse_iso(data.get("start_at_iso"))

        reminder = add_reminder(str(task_id), task_title, interval, times, start_at=start_dt.isoformat() if start_dt else None)

        start_fmt = ""
        if start_dt:
            start_fmt = f"\n📅 Начало: {start_dt.strftime('%d.%m.%Y %H:%M')}"
        interval_str = format_interval(interval, times) if interval > 0 else "однократно"
        await update.message.reply_text(
            f"🔔 *Напоминание установлено!*\n\n"
            f"📌 {_esc_md(task_title)}\n"
            f"⏱ {interval_str}, {format_times_left(reminder.get('times_left', times))}{start_fmt}",
            parse_mode="Markdown"
        )
        return

    if not mode:
        await handle_free_text(update, context, text)
        return

    # Остальные режимы (edit_title, edit_deadline, from_msg_*)
    await _handle_mode(update, context, mode, text)


async def _handle_mode(update, context, mode: str, text: str):
    """Обрабатываем активный режим редактирования."""
    from storage import save_tasks
    from data_lock import file_lock
    from config import TASKS_FILE

    if mode == "edit_title":
        task_id = context.user_data.get("_edit_task_id")
        back_filter = context.user_data.get("_edit_back_filter", "all")
        context.user_data.pop("_mode", None)
        # file_lock — без него конкурентная фоновая sync_all_tasks (сама уже
        # под локом) могла бы перезаписать этот save_tasks() между чтением и
        # записью и потерять правку пользователя (или наоборот).
        with file_lock(TASKS_FILE):
            tasks = get_tasks()
            for t in tasks:
                if str(t["id"]) == str(task_id):
                    t["title"] = text
                    save_tasks(tasks)
                    found = True
                    break
            else:
                found = False
        if found:
            await update.message.reply_text(f"✅ *Название обновлено!*\n\n📌 {text}", parse_mode="Markdown")
            await show_tasks(update.message, back_filter)
            return
        await update.message.reply_text("❌ Задача не найдена")

    elif mode == "edit_deadline":
        task_id = context.user_data.get("_edit_task_id")
        back_filter = context.user_data.get("_edit_back_filter", "all")

        if text.lower() in ["без даты", "нет", "-"]:
            context.user_data.pop("_mode", None)
            with file_lock(TASKS_FILE):
                tasks = get_tasks()
                for t in tasks:
                    if str(t["id"]) == str(task_id):
                        t["deadline"] = None
                        save_tasks(tasks)
                        found = True
                        break
                else:
                    found = False
            if found:
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
        with file_lock(TASKS_FILE):
            tasks = get_tasks()
            for t in tasks:
                if str(t["id"]) == str(task_id):
                    t["deadline"] = dt.isoformat()
                    save_tasks(tasks)
                    found = True
                    break
            else:
                found = False
        if found:
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
                f"✅ *Задача добавлена без даты!*\n\n📌 {_esc_md(task['title'])}",
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
            f"✅ *Задача добавлена!*\n\n📌 {_esc_md(task['title'])}\n⏰ {dt.strftime('%d.%m.%Y')}",
            parse_mode="Markdown"
        )


# ─── Callback кнопок ─────────────────────────────────────────────────

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # Текстовые/командные хендлеры проверяют владельца, а кнопки — нет:
    # без этой проверки любой, кому бот хоть раз ответил кнопками, мог бы
    # закрывать задачи/напоминания через callback_data.
    if not is_authorized(update.effective_user.id):
        await query.answer()
        return
    await query.answer()
    data = query.data

    if data.startswith("wundo:"):
        from html import escape as _html_escape
        # "↩️ Вернуть" из уведомления о закрытии задачи галочкой на столе.
        from task_actions import undo_action
        result = undo_action(data.split(":", 1)[1])
        await query.edit_message_reply_markup(reply_markup=None)
        if result["ok"]:
            await query.message.reply_text(
                f"↩️ Вернул: <b>{_html_escape(result['title'])}</b>", parse_mode="HTML",
            )
        else:
            await query.message.reply_text(f"Не получилось вернуть: {result['error']}")
        return

    if data.startswith("pc:"):
        # Наблюдение из недельного итога: в память — только после подтверждения.
        from agent_db import decide_profile_claim
        try:
            _, cid, status = data.split(":")
            row = decide_profile_claim(int(cid), status)
        except ValueError:
            row = None
        await query.edit_message_reply_markup(reply_markup=None)
        if row:
            await query.message.reply_text("🧠 Запомнил." if status == "confirmed" else "Ок, не запоминаю.")
        return

    if data.startswith("xt:"):
        # Срок, найденный в письме/сообщении (scripts/agent_extract.py).
        import sys as _sys, os as _os
        _sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "scripts"))
        from agent_extract import accept_proposal, reject_proposal
        from html import escape as _h
        _, eid, action = (data.split(":") + ["", ""])[:3]
        await query.edit_message_reply_markup(reply_markup=None)
        if action == "add":
            task = accept_proposal(int(eid))
            if task:
                await query.message.reply_text(f"✅ Завёл задачу: <b>{_h(task['title'])}</b>", parse_mode="HTML")
            else:
                await query.message.reply_text("Уже обработано.")
        else:
            reject_proposal(int(eid))
        return

    if data.startswith("deep:"):
        # «🔎 Подробнее в материалах» под ответом из сводки.
        question = context.user_data.pop(data.split(":", 1)[1], None)
        await query.edit_message_reply_markup(reply_markup=None)
        if not question:
            await query.message.reply_text("Вопрос устарел — задай его ещё раз.")
            return
        from bot.smart_intent import deep_answer_text, _split_message
        from agent_db import add_dialog_message
        msg = await query.message.reply_text("🔎 Ищу в материалах курса…")
        answer = await deep_answer_text(question)
        add_dialog_message("assistant", answer)
        import re as _re
        for i, chunk in enumerate(_split_message(answer)):
            try:
                if i == 0:
                    await msg.edit_text(chunk, parse_mode="HTML")
                else:
                    await query.message.reply_text(chunk, parse_mode="HTML")
            except Exception:
                plain = _re.sub(r"</?[a-zA-Z][^>]*>", "", chunk)
                await (msg.edit_text(plain) if i == 0 else query.message.reply_text(plain))
        return

    if data.startswith("cm:"):
        # Обещания: подтверждение кандидата / закрытие открытого (agent_db).
        from html import escape as _h
        from agent_db import set_commitment_status
        try:
            _, cid, status = data.split(":")
            row = set_commitment_status(int(cid), status)
        except ValueError:
            row = None
        await query.edit_message_reply_markup(reply_markup=None)
        if not row:
            await query.message.reply_text("Это обещание уже обработано.")
        else:
            labels = {"open": "📌 Записал", "rejected": "Ок, не записываю",
                      "done": "✅ Выполнено", "cancelled": "✖️ Снято"}
            await query.message.reply_text(f"{labels.get(status, status)}: <b>{_h(row['action'])}</b>",
                                           parse_mode="HTML")
        return

    if data.startswith("mfb:"):
        # 👍/👎 под сообщением наставника — журнал полезности (калибровка
        # тем и частоты сообщений по реальной реакции, а не на глаз).
        import json as _json
        _, fid, rating = (data.split(":") + ["", ""])[:3]
        try:
            with open("data/mentor_feedback.jsonl", "a", encoding="utf-8") as f:
                f.write(_json.dumps({
                    "at": datetime.datetime.now(tz=UFA_TZ).isoformat(timespec="seconds"),
                    "feedback_id": fid, "rating": rating,
                }, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"mentor feedback write error: {e!r}")
        await query.edit_message_reply_markup(reply_markup=None)
        return

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
        # Через общий путь закрытия (task_actions): снимаются и напоминания
        # задачи, и есть откат — раньше mark_task_done напрямую оставлял
        # напоминания звонить (ревью Codex 2026-10-02).
        from task_actions import complete_task
        count = sum(1 for tid in selected if complete_task(tid, origin="bot_batch_done")["ok"])
        context.user_data["done_selected"] = []
        await query.edit_message_reply_markup(reply_markup=None)

        streak_msg = ""

        await query.message.reply_text(
            f"✅ Отмечено выполненными: *{count}*{streak_msg}",
            parse_mode="Markdown"
        )
        await show_tasks(query.message, back_filter)

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
        from data_lock import file_lock
        from config import TASKS_FILE
        # file_lock — один лок на обе правки tasks.json ниже (основное удаление
        # + удаление связанных reminder_only задач), иначе конкурентная фоновая
        # sync_all_tasks могла бы вклиниться между ними.
        with file_lock(TASKS_FILE):
            tasks = get_tasks()
            # Связанные reminder_only задачи ищем ДО того, как основной
            # список отфильтрован — иначе (как было раньше) они бы уже не
            # находились в уже отфильтрованном списке и это удаление
            # оставалось бы мёртвым кодом.
            reminder_only_ids = {
                str(t["id"]) for t in tasks
                if t.get("source") == "reminder_only" and str(t["id"]) in selected
            }
            tasks = [t for t in tasks if str(t["id"]) not in selected and str(t["id"]) not in reminder_only_ids]
            save_tasks(tasks)
        # Удаляем связанные напоминания
        try:
            from reminders import _load, _save, REMINDERS_FILE
            from data_lock import file_lock as _file_lock
            with _file_lock(REMINDERS_FILE):
                reminders = _load()
                reminders = [r for r in reminders if str(r.get("task_id", "")) not in selected]
                _save(reminders)
        except Exception as e:
            print(f"Reminder cleanup error: {e!r}")
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
            f"✏️ *{_esc_md(task['title'][:50])}*\nЧто изменить?",
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
            await msg.edit_text(f"❌ Ошибка: {e!r}")

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
        await query.edit_message_reply_markup(reply_markup=None)
        if task_id:
            # Общая логика с окном наставника на столе: reminder_only
            # удаляется, обычная задача → done, снимаются ВСЕ её напоминания
            # (включая это — поэтому вручную его не удаляем: иначе оно не
            # попало бы в копию для отката).
            from task_actions import complete_task
            result = complete_task(task_id, origin="bot_reminder_button")
            if result["ok"]:
                await query.message.reply_text("✅ Готово, закрыто и напоминания сняты.")
            else:
                await query.message.reply_text(f"Не закрыто: {result['error']}")
        elif rem_id:
            from reminders import delete_reminder
            delete_reminder(rem_id)
            await query.message.reply_text("✅ Напоминание удалено.")

    elif data.startswith("rem_delete:"):
        # Удалить напоминание и связанную reminder_only задачу
        rem_id = data.split(":")[1]
        from reminders import delete_reminder, _load
        # Находим task_id до удаления
        rems = _load()
        rem_obj = next((r for r in rems if r["id"] == rem_id), None)
        delete_reminder(rem_id)
        if rem_obj:
            tid = str(rem_obj.get("task_id", ""))
            if tid:
                from storage import save_tasks
                all_tasks = get_tasks()
                task_obj = next((t for t in all_tasks if str(t["id"]) == tid), None)
                if task_obj and task_obj.get("source") == "reminder_only":
                    all_tasks = [t for t in all_tasks if str(t["id"]) != tid]
                    save_tasks(all_tasks)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.answer("🗑 Напоминание удалено")

    elif data.startswith("rem_snooze:"):
        # Отложить напоминание на N минут
        parts = data.split(":")
        rem_id = parts[1]
        minutes = int(parts[2]) if len(parts) > 2 else 15
        from reminders import _load, _save, REMINDERS_FILE
        from data_lock import file_lock as _file_lock
        import datetime as _dt
        from config import UFA_TZ as _TZ
        with _file_lock(REMINDERS_FILE):
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
            f"🔔 *{_esc_md(task['title'][:50])}*\n\n"
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

    elif data.startswith("remind_del:"):
        reminder_id = data.split(":", 1)[1]
        from reminders import delete_reminder, _load as _load_rems
        rem_obj = next((r for r in _load_rems() if r.get("id") == reminder_id), None)
        rem_task = None
        if rem_obj:
            rem_task = next((t for t in get_tasks() if str(t.get("id")) == str(rem_obj.get("task_id"))), None)
        if rem_task and rem_task.get("source") == "reminder_only":
            # Личное напоминание закрывается целиком (задача + все её
            # напоминания) — иначе служебная задача осталась бы висеть на столе.
            from task_actions import complete_task
            complete_task(rem_task["id"], origin="bot_reminders_list")
        else:
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

    elif data.startswith("si_task:"):
        token = data.split(":", 1)[1]
        saved = context.user_data.pop(token, None)
        await query.edit_message_reply_markup(reply_markup=None)
        if not saved:
            await query.message.reply_text("❌ Данные устарели, напиши заново")
            return
        from bot.smart_intent import create_task, _parse_iso
        item = saved["item"]
        task = create_task(item.get("title") or saved["text"], item.get("deadline_iso"))
        dl = _parse_iso(item.get("deadline_iso"))
        dl_str = f" — {dl.strftime('%d.%m.%Y')}" if dl else ""
        await query.message.reply_text(
            f"✅ Задача: <b>{_esc_md(task['title'])}</b>{dl_str}", parse_mode="HTML",
        )

    elif data.startswith("si_reminder:"):
        token = data.split(":", 1)[1]
        saved = context.user_data.pop(token, None)
        await query.edit_message_reply_markup(reply_markup=None)
        if not saved:
            await query.message.reply_text("❌ Данные устарели, напиши заново")
            return
        from bot.smart_intent import create_one_off_reminder
        item = saved["item"]
        title = item.get("title") or saved["text"]
        _, rem_dt = create_one_off_reminder(
            title, item.get("reminder_at_iso") or item.get("deadline_iso"), item.get("event_date_iso"),
        )
        now = datetime.datetime.now(tz=UFA_TZ)
        time_fmt = rem_dt.strftime("%H:%M") if rem_dt.date() == now.date() else rem_dt.strftime("%d.%m %H:%M")
        await query.message.reply_text(
            f"🔔 Напомню в {time_fmt}: <b>{_esc_md(title)}</b>", parse_mode="HTML",
        )

    elif data.startswith("si_cancel:"):
        token = data.split(":", 1)[1]
        context.user_data.pop(token, None)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("❌ Отменено")

    elif data.startswith("si_done:"):
        task_id = data.split(":", 1)[1]
        await query.edit_message_reply_markup(reply_markup=None)
        from bot.smart_intent import complete_item
        ok, title = complete_item(task_id)
        if ok:
            await query.message.reply_text(f"✅ Завершено: <b>{_esc_md(title)}</b>", parse_mode="HTML")
        else:
            await query.message.reply_text("❌ Уже не найдено — возможно, кто-то опередил.")

    elif data == "si_done_none":
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Хорошо, ничего не трогаю.")

    elif data.startswith("schedule:") or data.startswith("sched_cache:") or data.startswith("sched_fresh:"):
        await handle_schedule_callback(update, context)


# ─── Команды ─────────────────────────────────────────────────────────

async def promises_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/promises — открытые обещания с кнопками «выполнено»/«снять»."""
    if not is_authorized(update.effective_user.id):
        return
    from html import escape as _h
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from agent_db import list_commitments
    rows = list_commitments("open", 20)
    if not rows:
        await update.message.reply_text("Открытых обещаний нет. Скажи боту что-то вроде «сделаю лабу в пятницу» — он предложит записать.")
        return
    today = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
    for r in rows:
        due = ""
        if r.get("due_at"):
            due = f" — до {r['due_at'][8:10]}.{r['due_at'][5:7]}" + (" ⚠️ срок прошёл" if r["due_at"][:10] < today else "")
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Выполнено", callback_data=f"cm:{r['id']}:done"),
            InlineKeyboardButton("✖️ Снять", callback_data=f"cm:{r['id']}:cancelled"),
        ]])
        await update.message.reply_text(f"📌 <b>{_h(r['action'])}</b>{due}\n<i>«{_h(r['quote'][:150])}»</i>",
                                        reply_markup=kb, parse_mode="HTML")


async def tokens_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tokens — расход Claude по журналу data/agent_calls.jsonl: сегодня и
    за 7 дней, по назначению вызова, плюс оценки 👍/👎 наставника."""
    if not is_authorized(update.effective_user.id):
        return
    import json as _json
    from collections import defaultdict
    now = datetime.datetime.now(tz=UFA_TZ)
    today, week = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
    try:
        with open("data/agent_calls.jsonl", encoding="utf-8") as f:
            rows = []
            for l in f:
                try:
                    rows.append(_json.loads(l))
                except _json.JSONDecodeError:
                    continue   # одна битая строка не ломает отчёт
    except FileNotFoundError:
        rows = []
    for r in rows:
        at = datetime.datetime.fromisoformat(r["at"])
        if (now - at).days < 7:
            week[r["purpose"]][0] += 1
            week[r["purpose"]][1] += r.get("total", 0)
            if at.date() == now.date():
                today[r["purpose"]][0] += 1
                today[r["purpose"]][1] += r.get("total", 0)

    def block(title, d):
        if not d:
            return f"<b>{title}:</b> вызовов не было"
        total = sum(v[1] for v in d.values())
        lines = [f"<b>{title}:</b> {total // 1000} тыс. токенов"]
        for k, (n, t) in sorted(d.items(), key=lambda kv: -kv[1][1]):
            lines.append(f"  • {k}: {n} × ≈{t // max(n, 1) // 1000} тыс. = {t // 1000} тыс.")
        return "\n".join(lines)

    up = down = 0
    try:
        with open("data/mentor_feedback.jsonl", encoding="utf-8") as f:
            for l in f:
                try:
                    r = _json.loads(l)
                except _json.JSONDecodeError:
                    continue
                if (now - datetime.datetime.fromisoformat(r["at"])).days < 7:
                    up += r.get("rating") == "up"
                    down += r.get("rating") == "down"
    except FileNotFoundError:
        pass
    text = "\n\n".join([
        "📊 <b>Расход Claude</b>",
        block("Сегодня", today),
        block("За 7 дней", week),
        f"<b>Оценки наставника за 7 дней:</b> 👍 {up} · 👎 {down}",
    ])
    await update.message.reply_text(text, parse_mode="HTML")

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
        from scheduler import get_ai_memory_recap, record_ai_memory
        streak_result = mark_active_today()
        mark_evening_reported()
        streak = streak_result["streak"]
        tasks = get_tasks()
        done_today = len([t for t in tasks if t.get("manually_done")])
        pending = len([t for t in tasks if not t.get("done")])
        grok_text = await grok_evening_analysis(report, done_today, pending, streak, get_ai_memory_recap())
        if grok_text:
            await record_ai_memory("itog", grok_text)
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
        await msg.edit_text(f"❌ Ошибка: {e!r}")


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
        await update.message.reply_text(f"❌ {e!r}")


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
        from scheduler import get_ai_memory_recap, record_ai_memory
        import json as _json

        raw = await fetch_study_analysis()
        memory_recap = get_ai_memory_recap()

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
        prompt = f"""{memory_recap + chr(10) + chr(10) if memory_recap else ""}Данные успеваемости на {now_str} (середина семестра):

{raw}

Сделай подробный аналитический отчёт. Раздели на [ЧАСТЬ1] и [ЧАСТЬ2]."""

        await msg.edit_text("🤖 Анализирую данные...")
        ai_text = await ask_grok(prompt, system=system, smart=False)

        if ai_text:
            await record_ai_memory("analysis", ai_text[:400])

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
        await msg.edit_text(f"❌ Ошибка: {e!r}")
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




_LAST_ERROR_ALERTS = {}  # текст ошибки -> unix-время последней отправки
_ERROR_ALERT_THROTTLE_SECONDS = 300


async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    """Раньше необработанная ошибка в хендлере просто терялась в stdout —
    админ узнавал о проблеме только случайно. Теперь хотя бы шлём себе алерт.
    Троттлим повторяющуюся ошибку (например, один и тот же баг на каждое
    входящее сообщение) — иначе можно заспамить себя же алертами."""
    import time
    import traceback
    err_text = "".join(traceback.format_exception(None, context.error, context.error.__traceback__))
    print(f"Необработанная ошибка в хендлере: {err_text}")
    short = str(context.error)[:300]

    now = time.time()
    last_sent = _LAST_ERROR_ALERTS.get(short, 0)
    if now - last_sent < _ERROR_ALERT_THROTTLE_SECONDS:
        return
    _LAST_ERROR_ALERTS[short] = now
    # Не даём словарю расти бесконечно при потоке разных ошибок
    if len(_LAST_ERROR_ALERTS) > 200:
        _LAST_ERROR_ALERTS.clear()
        _LAST_ERROR_ALERTS[short] = now

    try:
        await context.bot.send_message(
            chat_id=MY_TELEGRAM_ID,
            text=f"⚠️ Ошибка в боте: {short}",
        )
    except Exception as e:
        print(f"Не удалось отправить алерт об ошибке: {e!r}")


def register_handlers(app):
    app.add_error_handler(error_handler)

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
    # Раньше /add открывал двухшаговый диалог (ConversationHandler:
    # название → отдельно дедлайн). Убрано вместе со всей старой цепочкой
    # уточнений — add_command теперь просто прогоняет текст (если он был
    # сразу после команды) через smart_intent.handle_free_text, как и любое
    # обычное сообщение.
    app.add_handler(CommandHandler("add", add_command))
    app.add_handler(CommandHandler("tokens", tokens_command))
    app.add_handler(CommandHandler("promises", promises_command))
    app.add_handler(MessageHandler(filters.Regex("^➕ Добавить задачу$"), add_command))

    app.add_handler(CallbackQueryHandler(button_callback))

    # Меню — ПЕРЕД mode_text_handler
    app.add_handler(MessageHandler(
        filters.Regex("^(📋 Задания|📅 Расписание|➕ Добавить задачу|🔄 Синхронизировать|🎓 Оценки|🔔 Напомнить)$"),
        menu_handler
    ))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mode_text_handler))
