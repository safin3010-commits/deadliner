"""
Умный мастер создания напоминаний.
"""
import datetime
import re
from config import UFA_TZ

DRAFT_KEY = "_rem_draft"

def _get_draft(context) -> dict:
    return context.user_data.get(DRAFT_KEY, {})

def _set_draft(context, draft: dict):
    context.user_data[DRAFT_KEY] = draft

def _clear_draft(context):
    context.user_data.pop(DRAFT_KEY, None)
    context.user_data.pop("_rem_wizard_step", None)


async def parse_reminder_intent(text: str, now: datetime.datetime) -> dict:
    from grok import ask_grok
    import json as _json
    import re as _re

    now_str = now.strftime('%d.%m.%Y')
    now_time = now.strftime('%H:%M')
    weekday = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"][now.weekday()]
    day1 = (now + datetime.timedelta(days=1)).strftime('%d.%m.%Y')
    day2 = (now + datetime.timedelta(days=2)).strftime('%d.%m.%Y')

    prompt = (
        "Текст пользователя: \"" + text + "\"\n"
        "Сейчас: " + now_str + " " + now_time + " (" + weekday + "). Завтра: " + day1 + ". Послезавтра: " + day2 + ".\n\n"
        "Проанализируй и верни JSON со следующими полями:\n"
        "- reminder_text: ТОЛЬКО суть действия без временных слов\n"
        "- date: дата DD.MM.YYYY или null\n"
        "- date_ambiguous: true если несколько дат упомянуто\n"
        "- date_options: список дат если date_ambiguous=true, иначе []\n"
        "- time_of_day: время HH:MM если явно указано, иначе null\n"
        "- time_known: true только если время ЯВНО указано\n"
        "- interval_minutes: минуты между повторами или null\n"
        "- interval_known: true только если интервал ЯВНО указан\n"
        "- times_count: количество повторов или null\n"
        "- times_known: true только если количество ЯВНО указано\n"
        "- is_recurring: true ТОЛЬКО если есть каждые/каждый/раз в. НЕ ставь для через X\n"
        "- raw_type: reminder | task | ambiguous\n\n"
        "КРИТИЧЕСКИ ВАЖНО:\n"
        "\"через 2 часа\" = однократно, is_recurring=false, interval_minutes=null\n"
        "\"каждые 2 часа\" = повтор, is_recurring=true, interval_minutes=120\n"
        "\"каждый день в 9\" = interval_minutes=1440, time_of_day=09:00, is_recurring=true\n"
        "\"завтра или послезавтра\" = date_ambiguous=true, date_options=[\"" + day1 + "\",\"" + day2 + "\"]\n\n"
        "ПРИМЕРЫ:\n"
        "\"напомни выпить таблетку через 2 часа\" -> is_recurring=false, interval_minutes=null\n"
        "\"каждые 30 минут 5 раз пить воду с 10 утра\" -> interval_minutes=30, interval_known=true, times_count=5, times_known=true, is_recurring=true\n"
        "\"каждый день в 9 утра делать зарядку\" -> interval_minutes=1440, interval_known=true, time_of_day=09:00, is_recurring=true\n"
        "\"напомни завтра или послезавтра позвонить врачу\" -> date_ambiguous=true, date_options=[\"" + day1 + "\",\"" + day2 + "\"]\n"
        "Только JSON без пояснений."
    )

    try:
        result = await ask_grok(prompt, system="Ты анализатор намерений. Отвечай только валидным JSON.")
        if not result:
            return {"error": "no_response"}
        result = _re.sub(r'```[a-z]*\n?', '', result).strip()
        match = _re.search(r'\{.*\}', result, _re.DOTALL)
        if not match:
            return {"error": "no_json"}
        try:
            return _json.loads(match.group())
        except _json.JSONDecodeError:
            return {"error": "invalid_json"}
    except Exception as e:
        print(f"parse_reminder_intent error: {e}")
        return {"error": str(e)}


def compute_first_dt(draft: dict, now: datetime.datetime):
    date_str = draft.get("date")
    time_str = draft.get("time_of_day") or "09:00"
    if not date_str:
        return None
    try:
        dt = datetime.datetime.strptime(date_str + " " + time_str, "%d.%m.%Y %H:%M").replace(tzinfo=UFA_TZ)
        if dt <= now:
            dt += datetime.timedelta(days=1)
        return dt
    except Exception:
        return None


def get_missing_field(draft: dict):
    if draft.get("date_ambiguous") and not draft.get("date"):
        return "date_choice"
    if not draft.get("date"):
        return "date"
    if not draft.get("time_known") and not draft.get("time_of_day"):
        return "time_of_day"
    # Если указано больше 1 повтора — это повторяющееся, нужен интервал
    times = draft.get("times_count")
    if times and int(times) > 1:
        draft["is_recurring"] = True
    if draft.get("is_recurring") and not draft.get("interval_known"):
        return "interval_minutes"
    if draft.get("interval_minutes") and not draft.get("times_known"):
        return "times_count"
    return None


def _make_keyboard(buttons):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    kb = [[InlineKeyboardButton(t, callback_data=c) for t, c in row] for row in buttons]
    return InlineKeyboardMarkup(kb)


def draft_summary(draft: dict) -> str:
    lines = ["🔔 *" + draft.get("reminder_text", "?") + "*\n"]
    if draft.get("date"):
        lines.append("📅 Дата: *" + draft["date"] + "*")
    if draft.get("time_of_day"):
        lines.append("🕐 Время: *" + draft["time_of_day"] + "*")
    iv = draft.get("interval_minutes")
    if iv:
        s = ("каждые " + str(iv) + " мин") if iv < 60 else ("каждый час" if iv == 60 else ("каждые " + str(iv//60) + " ч"))
        lines.append("⏱ Интервал: *" + s + "*")
    if draft.get("times_count"):
        lines.append("🔁 Повторов: *" + str(draft["times_count"]) + "*")
    return "\n".join(lines)


async def ask_date_choice(message, draft: dict):
    options = draft.get("date_options", [])
    text = draft_summary(draft) + "\n\n📅 Какую дату выбрать?"
    months = {"01":"января","02":"февраля","03":"марта","04":"апреля","05":"мая",
               "06":"июня","07":"июля","08":"августа","09":"сентября",
               "10":"октября","11":"ноября","12":"декабря"}
    buttons = []
    for d in options:
        try:
            p = d.split(".")
            label = str(int(p[0])) + " " + months.get(p[1], p[1])
        except Exception:
            label = d
        buttons.append((label, "rem_wiz:date:" + d))
    rows = [buttons] if len(buttons) <= 3 else [buttons[:2], buttons[2:]]
    rows.append([("✍️ Другая", "rem_wiz:date:custom"), ("❌ Отмена", "rem_wiz:cancel")])
    return await message.reply_text(text, reply_markup=_make_keyboard(rows), parse_mode="Markdown")


async def ask_date(message, draft: dict):
    now = datetime.datetime.now(tz=UFA_TZ)
    text = draft_summary(draft) + "\n\n📅 На какую дату?"
    d0 = now.strftime("%d.%m.%Y")
    d1 = (now + datetime.timedelta(days=1)).strftime("%d.%m.%Y")
    d2 = (now + datetime.timedelta(days=2)).strftime("%d.%m.%Y")
    buttons = [
        [("Сегодня", "rem_wiz:date:" + d0), ("Завтра", "rem_wiz:date:" + d1), ("Послезавтра", "rem_wiz:date:" + d2)],
        [("✍️ Другая дата", "rem_wiz:date:custom"), ("❌ Отмена", "rem_wiz:cancel")],
    ]
    return await message.reply_text(text, reply_markup=_make_keyboard(buttons), parse_mode="Markdown")


async def ask_time(message, draft: dict):
    text = draft_summary(draft) + "\n\n🕐 В какое время начать?"
    buttons = [
        [("08:00", "rem_wiz:time:08:00"), ("09:00", "rem_wiz:time:09:00"), ("10:00", "rem_wiz:time:10:00")],
        [("12:00", "rem_wiz:time:12:00"), ("15:00", "rem_wiz:time:15:00"), ("18:00", "rem_wiz:time:18:00")],
        [("✍️ Другое", "rem_wiz:time:custom"), ("❌ Отмена", "rem_wiz:cancel")],
    ]
    return await message.reply_text(text, reply_markup=_make_keyboard(buttons), parse_mode="Markdown")


async def ask_interval(message, draft: dict):
    text = draft_summary(draft) + "\n\n⏱ Как часто напоминать?"
    buttons = [
        [("15 мин", "rem_wiz:interval:15"), ("30 мин", "rem_wiz:interval:30"), ("1 час", "rem_wiz:interval:60")],
        [("2 часа", "rem_wiz:interval:120"), ("3 часа", "rem_wiz:interval:180"), ("1 день", "rem_wiz:interval:1440")],
        [("Однократно", "rem_wiz:interval:0"), ("✍️ Другой", "rem_wiz:interval:custom"), ("❌ Отмена", "rem_wiz:cancel")],
    ]
    return await message.reply_text(text, reply_markup=_make_keyboard(buttons), parse_mode="Markdown")


async def ask_times_count(message, draft: dict):
    text = draft_summary(draft) + "\n\n🔁 Сколько раз напомнить?"
    buttons = [
        [("1 раз", "rem_wiz:times:1"), ("3 раза", "rem_wiz:times:3"), ("5 раз", "rem_wiz:times:5")],
        [("10 раз", "rem_wiz:times:10"), ("20 раз", "rem_wiz:times:20"), ("✍️ Другое", "rem_wiz:times:custom")],
        [("❌ Отмена", "rem_wiz:cancel")],
    ]
    return await message.reply_text(text, reply_markup=_make_keyboard(buttons), parse_mode="Markdown")


async def show_confirmation(message, draft: dict):
    now = datetime.datetime.now(tz=UFA_TZ)
    first_dt = compute_first_dt(draft, now)
    interval = draft.get("interval_minutes", 0)
    times = draft.get("times_count", 1)
    if first_dt:
        time_fmt = first_dt.strftime("%H:%M") if first_dt.date() == now.date() else first_dt.strftime("%d.%m в %H:%M")
    else:
        time_fmt = "?"
    if interval and interval > 0:
        s = ("каждые " + str(interval) + " мин") if interval < 60 else ("каждый час" if interval == 60 else ("каждые " + str(interval//60) + " ч"))
        repeat_str = s + ", " + str(times) + " раз"
    else:
        repeat_str = "однократно"
    text = (
        "✅ *Всё понял! Создаю напоминание:*\n\n"
        "📌 " + draft.get("reminder_text", "?") + "\n"
        "🕐 Первое: *" + time_fmt + "*\n"
        "🔁 " + repeat_str
    )
    buttons = [
        [("✅ Создать", "rem_wiz:confirm"), ("✏️ Изменить", "rem_wiz:edit")],
        [("❌ Отмена", "rem_wiz:cancel")],
    ]
    return await message.reply_text(text, reply_markup=_make_keyboard(buttons), parse_mode="Markdown")


async def ask_next_question(message, draft: dict, context=None):
    missing = get_missing_field(draft)
    if missing == "date_choice":
        sent = await ask_date_choice(message, draft)
    elif missing == "date":
        sent = await ask_date(message, draft)
    elif missing == "time_of_day":
        sent = await ask_time(message, draft)
    elif missing == "interval_minutes":
        sent = await ask_interval(message, draft)
    elif missing == "times_count":
        sent = await ask_times_count(message, draft)
    else:
        sent = await show_confirmation(message, draft)
    if context is not None and sent is not None:
        msgs = context.user_data.get("_rem_wiz_msgs", [])
        msgs.append(sent.message_id)
        context.user_data["_rem_wiz_msgs"] = msgs


async def create_reminder_from_draft(draft: dict) -> tuple:
    from storage import add_task
    from reminders import add_reminder
    now = datetime.datetime.now(tz=UFA_TZ)
    first_dt = compute_first_dt(draft, now)
    if not first_dt:
        raise ValueError("Не удалось вычислить время напоминания")
    rem_text = draft.get("reminder_text", "Напоминание")
    interval = draft.get("interval_minutes", 0)
    times = draft.get("times_count", 1)
    task = add_task(rem_text, first_dt.isoformat(), "reminder_only")
    if interval and interval > 0:
        reminder = add_reminder(str(task["id"]), rem_text, interval, times, start_at=first_dt.isoformat())
    else:
        delay = max(1, int((first_dt - now).total_seconds() / 60))
        reminder = add_reminder(str(task["id"]), rem_text, delay, 1, start_at=first_dt.isoformat())
    return task, reminder, first_dt
