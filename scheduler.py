import asyncio
import datetime
import json
import os
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from config import UFA_TZ, PARSE_HOURS
from storage import get_pending_tasks, get_tasks, save_tasks

PENDING_NOTIFICATIONS_FILE = "data/pending_notifications.json"
JARVIS_QUEUE_FILE = "data/jarvis_queue.json"


async def _retry(coro_fn, attempts=3, delay=5):
    """Повторяем корутину до attempts раз при ошибке или пустом результате."""
    for i in range(attempts):
        try:
            result = await coro_fn()
            if result:
                return result
        except Exception as e:
            print(f"_retry: попытка {i+1}/{attempts} упала: {e}")
        if i < attempts - 1:
            await asyncio.sleep(delay)
    return None


def _jarvis_should_read(text: str) -> bool:
    """Определяем стоит ли передавать сообщение Джарвису."""
    skip = [
        "Выбери фильтр", "Выбери период", "Anti-Laziness Bot запущен",
        "Привет, Ильнур", "Отмечено выполненными", "Задание удалено",
        "Все задания", "Личные задачи", "Показано:", "Выбери задания",
        "Срочные", "Редактировать", "Удалить", "Отмена", "Сохранить",
        "Это домашнее задание", "Пропустить", "Заданий нет",
        "Выбери задания которые",
    ]
    if any(p in text for p in skip):
        return False
    # Английский и теория — читаем всегда независимо от длины
    always_read = ["АНГЛИЙСКИЙ", "ТЕОРИЯ ДНЯ", "Анекдот на ночь", "СЛОВО ДНЯ"]
    if any(p in text for p in always_read):
        return True
    if len(text) > 2000:
        return False
    return True


def _jarvis_write(text: str):
    """Пишем сообщение в очередь для Джарвиса (атомарная запись)."""
    if not _jarvis_should_read(text):
        return
    try:
        os.makedirs("data", exist_ok=True)
        try:
            with open(JARVIS_QUEUE_FILE) as f:
                queue = json.load(f)
        except Exception:
            queue = []
        queue.append({
            "text": text,
            "ts": datetime.datetime.now(tz=UFA_TZ).isoformat()
        })
        if len(queue) > 50:
            queue = queue[-50:]
        tmp = JARVIS_QUEUE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(queue, f, ensure_ascii=False)
        os.replace(tmp, JARVIS_QUEUE_FILE)
    except Exception as e:
        print(f"Jarvis queue error: {e}")
RANDOM_SCHEDULE_FILE = "data/random_reminders.json"
LESSON_REMINDERS_FILE = "data/lesson_reminders_sent.json"
SENT_NOTIFICATIONS_FILE = "data/sent_notifications.json"

def _clean_joke(text: str) -> str:
    """Убираем всё лишнее после основного текста — скобки, PS, подписи."""
    import re as _re
    if not text:
        return text
    # Убираем строки начинающиеся со скобок, P.S., (P.S., примечаний
    lines = text.split("\n")
    clean = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Стоп-паттерны — строки которые надо убрать
        if _re.match(r"^[\(\[\*]", stripped):
            break
        if _re.match(r"^(P\.S\.|PS|Примечание|Note:|Если|Можно|—|\*)", stripped, _re.IGNORECASE):
            break
        clean.append(line)
    result = "\n".join(clean).strip()
    # Обрезаем по последнему знаку препинания
    m = _re.search(r"[.!?»\"']+\s*$", result)
    if m:
        result = result[:m.end()].strip()
    return result or text



FACTS_FILE = "data/facts.json"
FACTS_INDEX_FILE = "data/facts_index.json"


def _get_next_fact() -> str:
    """Возвращает следующий факт по кругу."""
    try:
        with open(FACTS_FILE, encoding="utf-8") as f:
            facts = json.load(f)
        try:
            with open(FACTS_INDEX_FILE) as f:
                data = json.load(f)
            idx = data.get("index", 0) % len(facts)
        except Exception:
            idx = 0
        fact = facts[idx]
        # Сохраняем следующий индекс
        os.makedirs("data", exist_ok=True)
        with open(FACTS_INDEX_FILE, "w") as f:
            json.dump({"index": (idx + 1) % len(facts)}, f)
        return fact
    except Exception:
        return ""



def _notify_header(category: str) -> str:
    """Жирный заголовок категории для каждого уведомления."""
    return f"*{category}*\n\n"


DAILY_STATS_FILE = "data/daily_stats.json"


def _load_daily_stats() -> dict:
    try:
        with open(DAILY_STATS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_daily_stats(data: dict):
    os.makedirs("data", exist_ok=True)
    with open(DAILY_STATS_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def record_daily_stats(done_today: int, pending: int, passed_lessons: int):
    """Записываем статистику дня. Вызывается из send_daily_results."""
    stats = _load_daily_stats()
    today = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
    stats[today] = {
        "done": done_today,
        "pending": pending,
        "lessons": passed_lessons,
        "recorded_at": datetime.datetime.now(tz=UFA_TZ).isoformat(),
    }
    # Храним последние 90 дней
    if len(stats) > 90:
        keys = sorted(stats.keys())
        for k in keys[:-90]:
            del stats[k]
    _save_daily_stats(stats)


def get_weekly_done_avg() -> float:
    """Среднее выполненных задач за последние 7 дней."""
    stats = _load_daily_stats()
    today = datetime.datetime.now(tz=UFA_TZ).date()
    total = 0
    days = 0
    for i in range(7):
        d = (today - datetime.timedelta(days=i+1)).isoformat()
        if d in stats:
            total += stats[d].get("done", 0)
            days += 1
    return round(total / days, 1) if days else 0


def get_stats_summary() -> str:
    """Краткая сводка за последние 7 дней для промпта ИИ."""
    stats = _load_daily_stats()
    today = datetime.datetime.now(tz=UFA_TZ).date()
    lines = []
    for i in range(7):
        d = (today - datetime.timedelta(days=i+1)).isoformat()
        if d in stats:
            s = stats[d]
            lines.append(f"{d}: выполнено {s.get('done',0)}, осталось {s.get('pending',0)}, пар {s.get('lessons',0)}")
    return "\n".join(lines) if lines else "нет данных"




def _load_sent_notifications() -> set:
    try:
        with open(SENT_NOTIFICATIONS_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_sent_notifications(sent: set):
    os.makedirs("data", exist_ok=True)
    with open(SENT_NOTIFICATIONS_FILE, "w") as f:
        json.dump(list(sent), f, ensure_ascii=False)


def _is_notification_sent(key: str) -> bool:
    return key in _load_sent_notifications()


def _mark_notification_sent(key: str):
    sent = _load_sent_notifications()
    sent.add(key)
    # Храним не больше 1000 записей
    if len(sent) > 1000:
        sent = set(list(sent)[-1000:])
    _save_sent_notifications(sent)
DEADLINE_SENT_FILE = "data/sent_deadline_reminders.json"


# ─── Pending notifications ────────────────────────────────────────────

def _load_pending_notifications() -> list:
    try:
        with open(PENDING_NOTIFICATIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_pending_notifications(notifications: list):
    os.makedirs("data", exist_ok=True)
    with open(PENDING_NOTIFICATIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(notifications, f, ensure_ascii=False, indent=2)


def _add_pending_notification(chat_id: int, text: str, parse_mode: str = "Markdown"):
    pending = _load_pending_notifications()
    pending.append({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "created_at": datetime.datetime.now(tz=UFA_TZ).isoformat(),
        "attempts": 0,
    })
    _save_pending_notifications(pending)
    print(f"Scheduler: уведомление в очередь (всего: {len(pending)})")


async def send_with_retry(bot, chat_id: int, text: str, parse_mode: str = "Markdown", reply_markup=None, ignore_quiet_hours: bool = False):
    # Тихие часы 00:00-09:00 — не отправляем (кроме явного игнорирования)
    if not ignore_quiet_hours:
        now_h = datetime.datetime.now(tz=UFA_TZ).hour
        if 0 <= now_h < 9:
            print(f"Scheduler: тихие часы ({now_h}:xx) — сообщение отложено")
            _add_pending_notification(chat_id, text, parse_mode)
            return False
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)
        _jarvis_write(text)
        return True
    except Exception as e:
        print(f"Scheduler: не удалось отправить: {e}")
        _add_pending_notification(chat_id, text, parse_mode)
        return False


async def retry_pending_notifications(bot):
    pending = _load_pending_notifications()
    if not pending:
        return
    print(f"Scheduler: retry {len(pending)} уведомлений...")
    still_pending = []
    for n in pending:
        try:
            await bot.send_message(chat_id=n["chat_id"], text=n["text"], parse_mode=n.get("parse_mode", "Markdown"))
        except Exception:
            n["attempts"] = n.get("attempts", 0) + 1
            if n["attempts"] < 144:
                still_pending.append(n)
    _save_pending_notifications(still_pending)


# ─── Синхронизация расписания ─────────────────────────────────────────

async def _fetch_schedule_fresh_or_cache() -> list:
    """Загружаем расписание на сегодня — сначала свежее, при ошибке кэш."""
    try:
        from parsers.modeus import get_cached_jwt, get_person_id_from_jwt, get_schedule, _get_week_start, _load_schedule_cache, _save_schedule_cache, get_week_schedule
        import asyncio as _asyncio

        today = datetime.datetime.now(tz=UFA_TZ).date()
        week_start = today - datetime.timedelta(days=today.weekday())

        # Сбрасываем кэш текущей недели чтобы загрузить свежее
        cache = _load_schedule_cache()
        if week_start.isoformat() in cache:
            del cache[week_start.isoformat()]
            _save_schedule_cache(cache)

        jwt_token = await _asyncio.wait_for(get_cached_jwt(), timeout=25)
        person_id = get_person_id_from_jwt(jwt_token) if jwt_token else None
        if not person_id:
            raise Exception("нет person_id")

        modeus_schedule = await _asyncio.wait_for(get_schedule(jwt_token, person_id, today), timeout=25)
        print(f"Modeus: свежее расписание на сегодня — {len(modeus_schedule)} занятий")

        # Добавляем Нетологию
        try:
            from parsers.netology import fetch_netology_schedule_week
            week_start = today - datetime.timedelta(days=today.weekday())
            netology_week = await _asyncio.wait_for(
                fetch_netology_schedule_week(week_start), timeout=20
            )
            netology_today = netology_week.get(today.isoformat(), []) if isinstance(netology_week, dict) else []
            print(f"Нетология: занятий на сегодня — {len(netology_today)}")
        except Exception as ne:
            print(f"Нетология today error: {ne}")
            netology_today = []

        combined = sorted(modeus_schedule + netology_today, key=lambda x: x.get("start_time", ""))
        return combined

    except Exception as e:
        print(f"Modeus: свежая загрузка не удалась ({e}), берём кэш...")
        try:
            from parsers.modeus import fetch_schedule_today
            return await asyncio.wait_for(fetch_schedule_today(), timeout=15)
        except Exception as e2:
            print(f"Modeus: кэш тоже не удался: {e2}")
            return []


# ─── Синхронизация задач ─────────────────────────────────────────────

async def sync_all_tasks():
    """Синхронизируем LMS и Нетологию."""
    try:
        from parsers.lms import fetch_lms_deadlines
        from parsers.netology import fetch_netology_deadlines

        lms_result, netology_result = await asyncio.gather(
            fetch_lms_deadlines(),
            fetch_netology_deadlines(),
            return_exceptions=True
        )

        existing_tasks = get_tasks()
        existing_ids = {t.get("id") for t in existing_tasks}
        existing_keys = {(t.get("title", ""), t.get("course_name", ""), t.get("deadline", "")) for t in existing_tasks}
        added = 0

        # LMS
        if isinstance(lms_result, tuple):
            lms_tasks, completed_ids = lms_result
            from storage import mark_lms_tasks_done
            marked = mark_lms_tasks_done(completed_ids, lms_tasks or [])
            if marked:
                print(f"sync_all_tasks: помечено выполненными {marked} LMS задач")
        else:
            lms_tasks = lms_result if isinstance(lms_result, list) else []
            completed_ids = set()

        # Нетология
        netology_tasks = []
        if isinstance(netology_result, tuple):
            netology_tasks, _ = netology_result
        elif isinstance(netology_result, list):
            netology_tasks = netology_result

        for t in (lms_tasks or []) + (netology_tasks or []):
            key = (t.get("title", ""), t.get("course_name", ""), t.get("deadline", ""))
            if t.get("id") not in existing_ids and key not in existing_keys:
                existing_tasks.append(t)
                existing_ids.add(t.get("id"))
                existing_keys.add(key)
                added += 1

        save_tasks(existing_tasks)
        if added:
            print(f"Scheduler: добавлено {added} новых задач")

    except Exception as e:
        print(f"Scheduler sync error: {e}")


# ─── Утренний брифинг 9:00 ───────────────────────────────────────────


async def _get_study_analysis_short() -> str:
    """Короткий анализ учёбы для сводок (5-6 предложений)."""
    try:
        from parsers.study_analysis import fetch_study_analysis
        from grok import ask_grok
        raw = await fetch_study_analysis()
        prompt = (
            f"Данные успеваемости студента Ильнура:\n{raw}\n\n"
            f"Напиши короткий анализ — ровно 5-6 предложений. "
            f"Укажи лучший и худший предмет по баллам, "
            f"общую тенденцию и один конкретный совет. "
            f"Без воды, по-русски, без скобок."
        )
        result = await ask_grok(prompt, system="Ты академический аналитик. Отвечай кратко — строго 5-6 предложений.")
        return result or ""
    except Exception as e:
        print(f"Study analysis short error: {e}")
        return ""

# ─── Дневной брифинг 14:00 ───────────────────────────────────────────

async def send_midday_reminder(bot, chat_id: int):
    """14:00 — одно сообщение: оставшиеся пары + срочные дедлайны."""
    try:
        print("Scheduler: дневное напоминание...")
        await sync_all_tasks()

        schedule = await _retry(_fetch_schedule_fresh_or_cache) or []
        tasks = get_pending_tasks()
        now = datetime.datetime.now(tz=UFA_TZ)

        lines = ["🌞 *Дневная сводка*\n"]

        # Только будущие пары
        future_lessons = []
        if schedule:
            from bot.messages import _expand_and_sort, _lesson_emoji, _s
            for lesson in _expand_and_sort(schedule):
                try:
                    start_dt = datetime.datetime.fromisoformat(lesson["start"])
                    if start_dt.tzinfo is None:
                        start_dt = start_dt.replace(tzinfo=UFA_TZ)
                    if start_dt > now:
                        future_lessons.append(lesson)
                except Exception:
                    continue

        if future_lessons:
            lines.append("📅 *Оставшиеся пары:*")
            from bot.messages import _expand_and_sort, _lesson_emoji, _s
            for lesson in future_lessons:
                emoji = _lesson_emoji(lesson)
                name = _s(lesson.get("course_name")) or _s(lesson.get("name"))
                start = _s(lesson.get("start_time"))
                lines.append(f"  {emoji} {start} — {name}")
            lines.append("")
        else:
            lines.append("📅 Пар больше нет\n")

        # Срочные задачи ≤3 дней
        urgent = []
        for t in tasks:
            if not t.get("deadline"):
                continue
            try:
                dt = datetime.datetime.fromisoformat(t["deadline"]).astimezone(UFA_TZ)
                days = (dt - now).days
                if days <= 3:
                    urgent.append((days, t))
            except Exception:
                continue
        urgent.sort(key=lambda x: x[0])

        if urgent:
            lines.append("⚠️ *Срочные задачи:*")
            for days, t in urgent[:5]:
                from bot.messages import _short_course, _deadline_emoji, _format_date
                emoji = _deadline_emoji(t.get("deadline"))
                course = _short_course(t.get("course_name", ""))
                title = t.get("title", "")
                date = _format_date(t.get("deadline"))
                lines.append(f"  {emoji} *{course}* — {title} _{date}_")
        else:
            lines.append("✅ Срочных задач нет")

        await send_with_retry(bot, chat_id, "\n".join(lines))

    except Exception as e:
        print(f"Scheduler midday reminder error: {e}")


# ─── Вечерний брифинг 21:00 ──────────────────────────────────────────

async def send_evening_reminder(bot, chat_id: int):
    """21:00 — одно сообщение: анализ дня + что завтра."""
    try:
        print("Scheduler: вечернее напоминание...")
        await sync_all_tasks()

        tasks = get_tasks()
        tasks_pending = [t for t in tasks if not t.get("done")]
        tasks_done = [t for t in tasks if t.get("done")]
        now = datetime.datetime.now(tz=UFA_TZ)

        # Завтрашние дедлайны
        tomorrow = (now + datetime.timedelta(days=1)).date()
        tomorrow_tasks = []
        for t in tasks_pending:
            try:
                d = datetime.datetime.fromisoformat(t["deadline"]).astimezone(UFA_TZ).date()
                if d == tomorrow:
                    tomorrow_tasks.append(t)
            except Exception:
                continue

        lines = ["🌙 *Итог дня*\n"]
        lines.append(f"✅ Выполнено всего: *{len(tasks_done)}*")
        lines.append(f"📋 Осталось: *{len(tasks_pending)}*")

        if tomorrow_tasks:
            lines.append("\n⚠️ *Завтра дедлайн:*")
            for t in tomorrow_tasks:
                from bot.messages import _short_course
                course = _short_course(t.get("course_name", ""))
                lines.append(f"  🔴 {course} — {t['title']}")

        # Groq анализ + завтра
        try:
            from grok import ask_grok
            schedule_tomorrow = await _fetch_tomorrow_schedule()
            sched_str = ""
            if schedule_tomorrow:
                from bot.messages import _s, _lesson_emoji
                pairs = [f"{_s(l.get('start_time'))} {_s(l.get('course_name'))}" for l in schedule_tomorrow[:3]]
                sched_str = f"Завтра пары: {', '.join(pairs)}. "

            urgent_str = ""
            if tomorrow_tasks:
                urgent_str = f"Завтра дедлайн: {', '.join(t['title'][:20] for t in tomorrow_tasks[:3])}. "

            prompt = (
                f"Осталось задач: {len(tasks_pending)}. "
                f"{sched_str}{urgent_str}"
                f"Напиши 2 предложения: мотивирующий итог дня и что важно сделать завтра. Без выдуманных фактов."
            )
            grok_text = await ask_grok(prompt)
            if grok_text:
                lines.append(f"\n🤖 {grok_text}")
        except Exception as e:
            print(f"Groq evening error: {e}")

        await send_with_retry(bot, chat_id, "\n".join(lines))

    except Exception as e:
        print(f"Scheduler evening reminder error: {e}")


async def _fetch_tomorrow_schedule() -> list:
    """Расписание на завтра — Modeus + Нетология."""
    try:
        from parsers.modeus import _load_schedule_cache, get_week_schedule
        from parsers.netology import fetch_netology_schedule_week
        tomorrow = (datetime.datetime.now(tz=UFA_TZ) + datetime.timedelta(days=1)).date()
        week_start = tomorrow - datetime.timedelta(days=tomorrow.weekday())

        # Modeus — сначала кэш, потом живой запрос
        modeus_tomorrow = []
        cache = _load_schedule_cache()
        entry = cache.get(week_start.isoformat())
        if entry:
            modeus_tomorrow = entry.get("data", {}).get(tomorrow.isoformat(), [])
        if not modeus_tomorrow:
            print("_fetch_tomorrow_schedule: кэша нет, запрашиваем Modeus...")
            week_data = await asyncio.wait_for(get_week_schedule(week_start), timeout=25)
            modeus_tomorrow = week_data.get(tomorrow.isoformat(), [])

        # Нетология
        netology_tomorrow = []
        try:
            netology_week = await asyncio.wait_for(
                fetch_netology_schedule_week(week_start), timeout=20
            )
            netology_tomorrow = netology_week.get(tomorrow.isoformat(), [])
        except Exception as ne:
            print(f"_fetch_tomorrow_schedule netology error: {ne}")

        combined = sorted(modeus_tomorrow + netology_tomorrow, key=lambda x: x.get("start_time", ""))
        return combined
    except Exception as e:
        print(f"_fetch_tomorrow_schedule error: {e}")
        return []


# ─── Проверка дедлайнов ───────────────────────────────────────────────

def _load_sent_deadlines() -> dict:
    try:
        with open(DEADLINE_SENT_FILE) as f:
            data = json.load(f)
        today = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
        if data.get("date") != today:
            return {"date": today, "sent": []}
        return data
    except Exception:
        today = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
        return {"date": today, "sent": []}


def _save_sent_deadlines(data: dict):
    os.makedirs("data", exist_ok=True)
    with open(DEADLINE_SENT_FILE, "w") as f:
        json.dump(data, f)


async def check_deadline_reminders(bot, chat_id: int):
    try:
        from bot.messages import deadline_reminder
        tasks = get_pending_tasks()
        now = datetime.datetime.now(tz=UFA_TZ)
        sent_data = _load_sent_deadlines()
        sent = sent_data["sent"]
        changed = False
        for task in tasks:
            try:
                if not task.get("deadline"):
                    continue
                deadline = datetime.datetime.fromisoformat(task["deadline"])
                days_left = (deadline - now).days
                if days_left not in [7, 3, 1]:
                    continue
                key = f"{task.get('id')}_{days_left}"
                if key in sent:
                    continue
                text = deadline_reminder(task, days_left)
                await send_with_retry(bot, chat_id, text)
                sent.append(key)
                changed = True
            except Exception:
                continue
        if changed:
            sent_data["sent"] = sent
            _save_sent_deadlines(sent_data)
    except Exception as e:
        print(f"Scheduler deadline reminders error: {e}")


# ─── Оценки ──────────────────────────────────────────────────────────

async def check_grades_and_notify(bot, chat_id: int):
    try:
        from parsers.lms import fetch_lms_deadlines
        from parsers.modeus_grades import fetch_modeus_grades
        from bot.messages import format_grade_notification_new, new_grade_message

        try:
            lms_result = await fetch_lms_deadlines()
            if isinstance(lms_result, tuple):
                _, completed_ids = lms_result
            else:
                completed_ids = set()
            existing_tasks = get_tasks()
            marked = 0
            from storage import mark_lms_tasks_done
            marked = mark_lms_tasks_done(completed_ids)
            if marked:
                print(f"Scheduler grades: помечено выполненными из LMS: {marked}")
        except Exception as e:
            print(f"Scheduler grades LMS error: {e}")

        modeus_grades = await _retry(fetch_modeus_grades) or []
        for grade in modeus_grades:
            grade_key = f"modeus_grade:{grade.get('course','')[:30]}:{grade.get('value','')}:{grade.get('lesson_date','')[:10]}:{grade.get('id','')[-8:]}"
            if _is_notification_sent(grade_key):
                continue
            text = grade.get("_text") or format_grade_notification_new(grade)
            sent_ok = await send_with_retry(bot, chat_id, _notify_header("🎓 Новая оценка — Modeus") + text)
            if sent_ok:
                _mark_notification_sent(grade_key)
                # Сохраняем seen только после успешной отправки
                if "_seen" in grade:
                    from parsers.modeus_grades import _save_seen
                    _save_seen(grade["_seen"])

    except Exception as e:
        print(f"Scheduler grades check error: {e}")


# ─── Почта и мессенджер ───────────────────────────────────────────────

async def check_mail_and_notify(bot, chat_id: int):
    try:
        from parsers.mail import fetch_new_emails
        from bot.messages import new_email_message
        from bot.keyboards import task_from_message_keyboard
        from storage import add_seen_message
        emails = await fetch_new_emails()
        for email_data in emails:
            try:
                from grok import beautify_message
                email_data["body"] = await beautify_message(
                    email_data.get("sender", ""), email_data.get("body", ""), "letter"
                )
            except Exception:
                pass
            # Помечаем сразу — чтобы не дублировать даже в тихие часы
            add_seen_message(email_data["id"])
            text = "<b>📧 Яндекс Почта</b>\n\n" + new_email_message(email_data)
            keyboard = task_from_message_keyboard(email_data["id"])
            await send_with_retry(bot, chat_id, text, parse_mode="HTML", reply_markup=keyboard)

    except Exception as e:
        print(f"Scheduler mail check error: {e}")


async def check_messenger_and_notify(bot, chat_id: int):
    try:
        from parsers.messenger import fetch_new_messages
        from bot.messages import new_messenger_message
        from bot.keyboards import task_from_message_keyboard
        from storage import add_seen_message
        messages = await fetch_new_messages()
        for msg in messages:
            try:
                from grok import beautify_message
                msg["text"] = await beautify_message(
                    msg.get("sender", ""), msg.get("text") or msg.get("preview", ""), "messenger"
                )
            except Exception:
                pass
            # Помечаем сразу — чтобы не дублировать даже в тихие часы
            add_seen_message(msg["id"])
            text = "<b>💬 Яндекс Мессенджер</b>\n\n" + new_messenger_message(msg)
            keyboard = task_from_message_keyboard(msg["id"])
            await send_with_retry(bot, chat_id, text, parse_mode="HTML", reply_markup=keyboard)

    except Exception as e:
        print(f"Scheduler messenger check error: {e}")


# ─── Напоминания пользователя ─────────────────────────────────────────

async def check_user_reminders(bot, chat_id: int):
    """Проверяем пользовательские напоминания каждые 5 минут."""
    try:
        now = datetime.datetime.now(tz=UFA_TZ)
        now_h = now.hour
        if 0 <= now_h < 9 or now_h >= 23:
            return  # Тихие часы

        from reminders import get_due_reminders, mark_sent, delete_reminder
        from storage import get_tasks
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        due = get_due_reminders()
        tasks = get_tasks()
        tasks_by_id = {str(t["id"]): t for t in tasks}

        for r in due:
            task_id = str(r.get("task_id", ""))
            task_obj = tasks_by_id.get(task_id)

            # П.5 — если задача выполнена — удаляем напоминание
            if task_obj and task_obj.get("done"):
                delete_reminder(r["id"])
                print(f"Reminder: задача выполнена, удаляем напоминание {r['id']}")
                continue

            # П.1 — дедлайн в тексте
            deadline_line = ""
            if task_obj and task_obj.get("deadline"):
                try:
                    dl = datetime.datetime.fromisoformat(task_obj["deadline"]).astimezone(UFA_TZ)
                    days_left = (dl.date() - now.date()).days
                    if days_left == 0:
                        deadline_line = "\n📅 Дедлайн: _сегодня!_"
                    elif days_left == 1:
                        deadline_line = "\n📅 Дедлайн: _завтра_"
                    elif days_left > 0:
                        deadline_line = f"\n📅 Дедлайн: _через {days_left} дн. ({dl.strftime('%d.%m')})_"
                    else:
                        deadline_line = f"\n📅 Дедлайн: _просрочен ({dl.strftime('%d.%m')})_"
                except Exception:
                    pass

            # П.3 — время следующего напоминания
            times_left_after = r["times_left"] - 1
            next_line = ""
            if times_left_after > 0 and r.get("interval_minutes", 0) > 0:
                next_dt = now + datetime.timedelta(minutes=r["interval_minutes"])
                next_line = f"\n⏭ {next_dt.strftime('%d.%m %H:%M')} (×{times_left_after})"
            elif times_left_after == 0:
                next_line = "\n_Это последнее напоминание_"

            msg_text = (
                f"🔔 *Напоминание*\n\n"
                f"📌 {r['task_title']}"
                f"{deadline_line}"
                f"{next_line}"
            )

            # П.2 — кнопки в уведомлении
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Выполнено", callback_data=f"rem_done:{task_id}:{r['id']}"),
                InlineKeyboardButton("⏭ Пропустить", callback_data=f"rem_skip:{r['id']}"),
            ]])

            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=msg_text,
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
                _jarvis_write(msg_text)
            except Exception as e:
                print(f"Reminder send error: {e}")

            mark_sent(r["id"])

    except Exception as e:
        print(f"Scheduler user reminders error: {e}")


# ─── Рандомные мотивационные ─────────────────────────────────────────


# ─── Напоминание о паре ───────────────────────────────────────────────

def _load_sent_lesson_reminders() -> set:
    try:
        with open(LESSON_REMINDERS_FILE) as f:
            data = json.load(f)
            today = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
            if data.get("date") != today:
                return set()
            return set(data.get("sent", []))
    except Exception:
        return set()


def _save_sent_lesson_reminders(sent: set):
    today = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
    os.makedirs("data", exist_ok=True)
    with open(LESSON_REMINDERS_FILE, "w") as f:
        json.dump({"date": today, "sent": list(sent)}, f)


async def check_lesson_reminders(bot, chat_id: int):
    """Каждые 5 минут — проверяем не начнётся ли пара через ~15 минут."""
    try:
        from parsers.modeus import fetch_schedule_today

        now = datetime.datetime.now(tz=UFA_TZ)
        if now.hour < 7 or now.hour >= 22:
            return

        schedule = await fetch_schedule_today()
        if not schedule:
            return

        sent = _load_sent_lesson_reminders()

        for lesson in schedule:
            try:
                start_dt = datetime.datetime.fromisoformat(lesson["start"])
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=UFA_TZ)

                minutes_until = (start_dt - now).total_seconds() / 60

                if not (13 <= minutes_until <= 17):
                    continue

                lesson_key = lesson.get("id") or lesson.get("start", "")
                if str(lesson_key) in sent:
                    continue

                name = lesson.get("course_name") or lesson.get("name", "Занятие")
                location = lesson.get("location", "")
                start_str = start_dt.strftime("%H:%M")

                text = f"🔔 *Пара через 15 минут!*\n\n📚 {name}\n🕐 Начало: {start_str}"
                if location:
                    text += f"\n📍 {location}"

                await send_with_retry(bot, chat_id, text)
                sent.add(str(lesson_key))
                _save_sent_lesson_reminders(sent)
                print(f"Scheduler: напоминание о паре \'{name}\' в {start_str}")

            except Exception as e:
                print(f"Scheduler lesson reminder error for lesson: {e}")

    except Exception as e:
        print(f"Scheduler lesson reminder error: {e}")


def _generate_random_times() -> list[str]:
    """
    3-4 случайных времени: 11:00–13:00 и 15:00–22:00.
    Если попадает рядом с 9/14 — сдвиг +3-40 мин.
    Минимум 3 часа между рандомными.
    """
    import random
    windows = [(11 * 60, 13 * 60), (15 * 60, 22 * 60)]
    min_gap = 180  # 3 часа
    times = []
    last = 8 * 60

    for w_start, w_end in windows:
        current = max(w_start, last + min_gap)
        while current + 20 <= w_end and len(times) < 4:
            jitter = random.randint(0, 20)
            t = current + jitter
            if t < w_end:
                h, m = divmod(t, 60)
                times.append(f"{h:02d}:{m:02d}")
                last = t
            current += min_gap + random.randint(0, 20)

    return times


def _load_random_schedule() -> dict:
    try:
        with open(RANDOM_SCHEDULE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_random_schedule(data: dict):
    os.makedirs("data", exist_ok=True)
    with open(RANDOM_SCHEDULE_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False)


def _get_todays_random_times() -> list[str]:
    today = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
    schedule = _load_random_schedule()
    if schedule.get("date") != today:
        times = _generate_random_times()
        _save_random_schedule({"date": today, "times": times, "sent": []})
        print(f"Scheduler: рандомные напоминания: {times}")
        return times
    return schedule.get("times", [])


def _mark_random_sent(time_str: str):
    today = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
    schedule = _load_random_schedule()
    if schedule.get("date") == today:
        sent = schedule.get("sent", [])
        if time_str not in sent:
            sent.append(time_str)
        schedule["sent"] = sent
        _save_random_schedule(schedule)


def _get_todays_motivation_time() -> str:
    """Рандомное время около 12:00 ±30 мин — генерируется раз в день."""
    import random as _random
    today = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
    schedule = _load_random_schedule()
    if schedule.get("motivation_date") == today and schedule.get("motivation_time"):
        return schedule["motivation_time"]
    # Генерируем: 11:30 — 12:30
    total_minutes = 11 * 60 + 30 + _random.randint(0, 60)
    h = total_minutes // 60
    m = total_minutes % 60
    t = f"{h:02d}:{m:02d}"
    schedule["motivation_date"] = today
    schedule["motivation_time"] = t
    _save_random_schedule(schedule)
    print(f"Scheduler: мотивация сегодня в {t}")
    return t


async def check_random_reminder(bot, chat_id: int):
    try:
        now = datetime.datetime.now(tz=UFA_TZ)
        if now.hour < 11 or now.hour >= 13:
            return

        motivation_time = _get_todays_motivation_time()
        schedule = _load_random_schedule()
        today = now.date().isoformat()

        # Уже отправляли мотивацию сегодня?
        if schedule.get("motivation_sent_date") == today:
            return

        th, tm = map(int, motivation_time.split(":"))
        target = datetime.datetime(now.year, now.month, now.day, th, tm, tzinfo=UFA_TZ)
        if abs((now - target).total_seconds()) <= 300:
            await _send_random_motivation(bot, chat_id)
            schedule["motivation_sent_date"] = today
            _save_random_schedule(schedule)
            print(f"Scheduler: мотивация отправлена в {motivation_time}")
    except Exception as e:
        print(f"Scheduler random reminder error: {e}")


async def _send_random_motivation(bot, chat_id: int):
    try:
        from grok import ask_grok
        from bot.messages import _short_course, _format_date
        tasks = get_pending_tasks()
        now = datetime.datetime.now(tz=UFA_TZ)

        # Находим самый срочный дедлайн
        urgent_task = None
        min_days = 9999
        for t in tasks:
            if not t.get("deadline"):
                continue
            try:
                dt = datetime.datetime.fromisoformat(t["deadline"]).astimezone(UFA_TZ)
                days = (dt - now).days
                if 0 <= days < min_days:
                    min_days = days
                    urgent_task = t
            except Exception:
                continue

        # Фраза от ИИ
        ai_line = ""
        if urgent_task:
            course = _short_course(urgent_task.get("course_name", ""))
            title = urgent_task.get("title", "")
            prompt = (
                f"Студент Ильнур, самая срочная задача: {course} — {title}, через {min_days} дн. "
                f"Напиши одну короткую мотивирующую фразу про эту задачу. "
                f"Без предисловий, без скобок. Только сама фраза."
            )
            ai_line = await ask_grok(prompt)

        # Цитата великого человека — ZenQuotes
        quote_text = ""
        quote_author = ""
        try:
            import httpx as _httpx
            async with _httpx.AsyncClient(timeout=8, follow_redirects=True) as _client:
                _r = await _client.get("https://zenquotes.io/api/random")
                if _r.status_code == 200:
                    _data = _r.json()[0]
                    quote_en = _data.get("q", "").strip()
                    quote_author = _data.get("a", "").strip()
                    if quote_en:
                        translated = await ask_grok(
                            f"Переведи эту мотивационную цитату на русский, сохрани смысл и стиль. Только перевод без кавычек и пояснений:\n{quote_en}"
                        )
                        quote_text = translated if translated else quote_en
        except Exception as e:
            print(f"ZenQuotes motivation error: {e}")

        lines = ["⚡️ *Не расслабляйся*", ""]

        if ai_line:
            lines.append(f"{ai_line}")
            lines.append("")

        if urgent_task:
            course = _short_course(urgent_task.get("course_name", ""))
            title = urgent_task.get("title", "")
            date = _format_date(urgent_task.get("deadline"))
            deadline_emoji = "🔴" if min_days == 0 else "🟡" if min_days <= 3 else "🟢"
            task_str = f"{course} — {title}" if course else title
            lines.append(f"{deadline_emoji} *{task_str}*")
            if date:
                lines.append(f"📅 _{date}_")
            lines.append("")

        if quote_text:
            lines.append(f"{'─' * 20}")
            lines.append(f"💬 {quote_text}")
            if quote_author:
                lines.append(f"— {quote_author}")

        await send_with_retry(bot, chat_id, "\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        print(f"Scheduler random motivation error: {e}")
async def send_weekly_report(bot, chat_id: int):
    try:
        from grok import ask_grok
        tasks = get_tasks()
        done = len([t for t in tasks if t.get("done")])
        pending = len([t for t in tasks if not t.get("done")])
        pct = int(done / max(done + pending, 1) * 100)

        await send_with_retry(bot, chat_id,
            f"📊 *Недельный отчёт*\n\n"
            f"✅ Выполнено: *{done}*\n"
            f"📋 Осталось: *{pending}*\n"
            f"📈 Выполнение: *{pct}%*"
        )

        prompt = (
            f"Итог недели: выполнено {done} из {done+pending} задач ({pct}%). "
            f"Напиши 2 предложения: оцени неделю и дай один конкретный совет на следующую."
        )
        grok_text = await ask_grok(prompt)
        if grok_text:
            await send_with_retry(bot, chat_id, f"🤖 {grok_text}")

    except Exception as e:
        print(f"Scheduler weekly report error: {e}")


# ─── Главные задачи планировщика ─────────────────────────────────────

async def sync_at_9(bot, chat_id: int):
    await send_morning_briefing(bot, chat_id)
    await check_deadline_reminders(bot, chat_id)


async def sync_at_14(bot, chat_id: int):
    await send_midday_reminder(bot, chat_id)
    await check_deadline_reminders(bot, chat_id)


async def sync_at_21(bot, chat_id: int):
    await send_evening_reminder(bot, chat_id)


# ─── Настройка планировщика ───────────────────────────────────────────


# ─── Утренний брифинг 9:00 (новый) ───────────────────────────────────


async def _fetch_weather() -> str:
    """Погода в Чесноковке — сейчас / днём / вечером через OpenWeatherMap."""
    try:
        import httpx
        lat, lon = 54.7355, 55.9578
        from config import OPENWEATHER_KEY
        api_key = OPENWEATHER_KEY

        async with httpx.AsyncClient(timeout=10) as client:
            r_cur = await client.get(
                "https://api.openweathermap.org/data/2.5/weather",
                params={"lat": lat, "lon": lon, "appid": api_key, "units": "metric", "lang": "ru"}
            )
            cur = r_cur.json()
            r_fc = await client.get(
                "https://api.openweathermap.org/data/2.5/forecast",
                params={"lat": lat, "lon": lon, "appid": api_key, "units": "metric", "lang": "ru", "cnt": 16}
            )
            fc = r_fc.json()

        def _icon(desc):
            d = desc.lower()
            if "ясно" in d: return "☀️"
            elif "малооблачно" in d: return "⛅"
            elif "облачно" in d or "пасмурно" in d: return "☁️"
            elif "гроза" in d: return "⛈️"
            elif "дождь" in d or "ливень" in d or "морось" in d: return "🌧️"
            elif "снег" in d or "метель" in d: return "❄️"
            elif "туман" in d: return "🌫️"
            else: return "🌤️"

        def _precip(item):
            rain = item.get("rain", {}).get("3h", 0)
            snow = item.get("snow", {}).get("3h", 0)
            if rain > 0: return f" 🌧️{rain:.1f}мм"
            if snow > 0: return f" ❄️{snow:.1f}мм"
            return ""

        now_temp = round(cur["main"]["temp"])
        now_feels = round(cur["main"]["feels_like"])
        now_desc = cur["weather"][0]["description"].capitalize()
        now_wind = round(cur["wind"]["speed"])
        now_gust = round(cur["wind"].get("gust", cur["wind"]["speed"]))
        now_rain = cur.get("rain", {}).get("1h", 0)
        now_snow = cur.get("snow", {}).get("1h", 0)
        now_precip = f" 🌧️{now_rain:.1f}мм" if now_rain > 0 else f" ❄️{now_snow:.1f}мм" if now_snow > 0 else ""

        day_item = None
        eve_item = None
        for item in fc.get("list", []):
            dt_txt = item.get("dt_txt", "")
            hour = int(dt_txt[11:13]) if len(dt_txt) >= 13 else 0
            if day_item is None and hour in (12, 13, 14, 15):
                day_item = item
            if eve_item is None and hour in (18, 19, 20, 21):
                eve_item = item

        out = []
        out.append(f"🌤 Сейчас {_icon(now_desc)} {now_temp}°C, {now_desc.lower()}, ветер {now_wind} м/с{now_precip}")

        if day_item:
            d_temp = round(day_item["main"]["temp"])
            d_desc = day_item["weather"][0]["description"].capitalize()
            d_wind = round(day_item["wind"]["speed"])
            d_precip = _precip(day_item)
            out.append(f"🌤 Днём {_icon(d_desc)} {d_temp}°C, {d_desc.lower()}, ветер {d_wind} м/с{d_precip}")

        if eve_item:
            e_temp = round(eve_item["main"]["temp"])
            e_desc = eve_item["weather"][0]["description"].capitalize()
            e_wind = round(eve_item["wind"]["speed"])
            e_precip = _precip(eve_item)
            out.append(f"🌤 Вечером {_icon(e_desc)} {e_temp}°C, {e_desc.lower()}, ветер {e_wind} м/с{e_precip}")

        return "\n".join(out)

    except Exception as e:
        print(f"Weather fetch error: {e}")
        return ""

MORNING_SENT_FILE = "data/morning_sent.json"


def _is_morning_sent() -> bool:
    try:
        with open(MORNING_SENT_FILE) as f:
            data = json.load(f)
        today = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
        return data.get("date") == today
    except Exception:
        return False


def _mark_morning_sent():
    today = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
    os.makedirs("data", exist_ok=True)
    with open(MORNING_SENT_FILE, "w") as f:
        json.dump({"date": today}, f)


async def send_morning_briefing(bot, chat_id: int):
    """9:00 — погода + расписание + дедлайны + Groq шутка. Не более 1 раза в день."""
    if _is_morning_sent():
        print("Scheduler: утренний брифинг уже был сегодня — пропускаем")
        return
    try:
        print("Scheduler: утренний брифинг 9:00...")
        _mark_morning_sent()
        await sync_all_tasks()
        from grok import ask_grok
        from bot.messages import _expand_and_sort, _lesson_emoji, _s, _short_course
        schedule = await _retry(_fetch_schedule_fresh_or_cache) or []
        tasks = get_pending_tasks()
        now = datetime.datetime.now(tz=UFA_TZ)
        MONTHS = ["января","февраля","марта","апреля","мая","июня","июля","августа","сентября","октября","ноября","декабря"]
        DAYS = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]
        DAY_NAMES = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]
        MONTHS = ["января","февраля","марта","апреля","мая","июня","июля","августа","сентября","октября","ноября","декабря"]
        SEP = "┄" * 20
        day_str = f"{DAY_NAMES[now.weekday()].capitalize()}, {now.day} {MONTHS[now.month-1]}"
        border = "═" * 26
        pad = "   "
        lines = [
            f"☀️ *Доброе утро, Ильнур!*",
            f"{day_str}",
            "",
        ]

        # Погода
        weather = await _fetch_weather()
        if weather:
            lines.append(weather)
            lines.append("")

        # Расписание
        if schedule:
            lines.append(f"*📅 ПАРЫ СЕГОДНЯ*")
            lines.append(SEP)
            for lesson in _expand_and_sort(schedule):
                emoji = _lesson_emoji(lesson)
                name = _s(lesson.get("course_name")) or _s(lesson.get("name"))
                start_t = _s(lesson.get("start_time"))
                lines.append(f"{emoji}  {start_t}  {name}")
        else:
            lines.append("📅 Пар сегодня нет 🎉")
        lines.append("")

        # Дедлайны
        urgent = []
        for t in tasks:
            if not t.get("deadline"):
                continue
            try:
                dt = datetime.datetime.fromisoformat(t["deadline"]).astimezone(UFA_TZ)
                days = (dt - now).days
                if 0 <= days <= 1:
                    urgent.append((days, t))
            except Exception:
                continue
        urgent.sort(key=lambda x: x[0])

        if urgent:
            has_today = any(d == 0 for d, _ in urgent)
            has_tomorrow = any(d == 1 for d, _ in urgent)
            if has_today and has_tomorrow:
                dl_label = "СЕГОДНЯ И ЗАВТРА"
            elif has_today:
                dl_label = "СЕГОДНЯ"
            else:
                dl_label = "ЗАВТРА"
            lines.append(f"*🔴 ДЕДЛАЙНЫ — {dl_label}*")
            lines.append(SEP)
            seen_titles = set()
            shown = 0
            for days, t in urgent:
                title = t.get("title", "")
                if title in seen_titles:
                    continue
                seen_titles.add(title)
                try:
                    dt = datetime.datetime.fromisoformat(t["deadline"]).astimezone(UFA_TZ)
                    date_str = dt.strftime("%d.%m")
                except Exception:
                    date_str = ""
                course = _short_course(t.get("course_name", ""))
                prefix = f"{course} — " if course else ""
                lines.append(f"❗  {prefix}{title}  •  {date_str}")
                shown += 1
                if shown >= 5:
                    break
        elif tasks:
            lines.append(f"*📚 ЗАДАНИЯ*")
            lines.append(SEP)
            lines.append(f"Всего: {len(tasks)}, срочных нет ✅")
            # Показываем ближайшее задание
            now_iso = now.isoformat()
            nearest = sorted(
                [t for t in tasks if t.get("deadline") and t["deadline"] >= now_iso],
                key=lambda x: x["deadline"]
            )
            if nearest:
                t = nearest[0]
                from bot.messages import _short_course
                course = _short_course(t.get("course_name", ""))
                try:
                    dt = datetime.datetime.fromisoformat(t["deadline"]).astimezone(UFA_TZ)
                    date_str = dt.strftime("%d.%m")
                except Exception:
                    date_str = ""
                prefix = f"{course} — " if course else ""
                lines.append(f"📌 Ближайшее: {prefix}{t['title'][:40]}  •  {date_str}")
        else:
            lines.append("✅ Все задания выполнены!")
        # Цитата дня на русском
        try:
            import httpx as _httpx
            async with _httpx.AsyncClient(timeout=8) as _client:
                _r = await _client.get(
                    "http://api.forismatic.com/api/1.0/",
                    params={"method": "getQuote", "lang": "ru", "format": "json"}
                )
                if _r.status_code == 200:
                    _data = _r.json()
                    _quote = _data.get("quoteText", "").strip()
                    _author = _data.get("quoteAuthor", "").strip()
                    if _quote:
                        lines.append(f"\n{'─' * 20}\n💬 {_quote}")
                        if _author:
                            lines.append(f"— {_author}")
        except Exception as e:
            print(f"Forismatic morning error: {e}")
        # Анализ учёбы
        try:
            study = await _get_study_analysis_short()
            if study:
                lines.append(f"\n{'─' * 20}\n📊 *УЧЁБА*\n{study}")
        except Exception as e:
            print(f"Morning study analysis error: {e}")

        await send_with_retry(bot, chat_id, "\n".join(lines))
    except Exception as e:
        print(f"Scheduler morning briefing error: {e}")
async def send_midday_briefing(bot, chat_id: int):
    """14:00 — дневная сводка."""
    try:
        print("Scheduler: дневной брифинг 14:00...")
        await sync_all_tasks()
        from grok import ask_grok
        from bot.messages import _lesson_emoji, _s, _short_course

        tasks = get_pending_tasks()
        now = datetime.datetime.now(tz=UFA_TZ)
        MONTHS = ["января","февраля","марта","апреля","мая","июня","июля","августа","сентября","октября","ноября","декабря"]
        DAYS = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]
        SEP = "┄" * 20
        border = "═" * 26
        pad = "   "

        lines = [
            f"🌞 *Добрый день, Ильнур!*",
            f"{DAYS[now.weekday()].capitalize()}, {now.day} {MONTHS[now.month-1]}",
            "",
        ]

        # Расписание сегодня
        from bot.messages import _expand_and_sort, _lesson_emoji, _s
        schedule = await _retry(_fetch_schedule_fresh_or_cache) or []
        if schedule:
            lines.append(f"*📅 ПАРЫ СЕГОДНЯ*")
            lines.append(SEP)
            for lesson in _expand_and_sort(schedule):
                emoji = _lesson_emoji(lesson)
                name = _s(lesson.get("course_name")) or _s(lesson.get("name"))
                start_t = _s(lesson.get("start_time"))
                lines.append(f"{emoji}  {start_t}  {name}")
            lines.append("")
        else:
            lines.append("📅 Пар сегодня нет 🎉")
            lines.append("")

        # Дедлайны сегодня + завтра
        urgent = []
        for t in tasks:
            if not t.get("deadline"):
                continue
            try:
                dt = datetime.datetime.fromisoformat(t["deadline"]).astimezone(UFA_TZ)
                days = (dt - now).days
                if 0 <= days <= 1:
                    urgent.append((days, t))
            except Exception:
                continue
        urgent.sort(key=lambda x: x[0])

        if urgent:
            has_today = any(d == 0 for d, _ in urgent)
            has_tomorrow = any(d == 1 for d, _ in urgent)
            if has_today and has_tomorrow:
                dl_label = "СЕГОДНЯ И ЗАВТРА"
            elif has_today:
                dl_label = "СЕГОДНЯ"
            else:
                dl_label = "ЗАВТРА"
            lines.append(f"*🔴 ДЕДЛАЙНЫ — {dl_label}*")
            lines.append(SEP)
            seen_titles = set()
            for days, t in urgent[:5]:
                title = t.get("title", "")
                if title in seen_titles:
                    continue
                seen_titles.add(title)
                try:
                    dt = datetime.datetime.fromisoformat(t["deadline"]).astimezone(UFA_TZ)
                    date_str = dt.strftime("%d.%m")
                except Exception:
                    date_str = ""
                course = _short_course(t.get("course_name", ""))
                prefix = f"{course} — " if course else ""
                lines.append(f"❗  {prefix}{title}  •  {date_str}")
            lines.append("")
        else:
            lines.append("✅ Срочных дедлайнов нет")
            lines.append("")

        # Цитата дня на русском
        try:
            import httpx as _httpx
            async with _httpx.AsyncClient(timeout=8) as _client:
                _r = await _client.get(
                    "http://api.forismatic.com/api/1.0/",
                    params={"method": "getQuote", "lang": "ru", "format": "json"}
                )
                if _r.status_code == 200:
                    _data = _r.json()
                    _quote = _data.get("quoteText", "").strip()
                    _author = _data.get("quoteAuthor", "").strip()
                    if _quote:
                        lines.append(f"{'─' * 20}")
                        lines.append(f"💬 {_quote}")
                        if _author:
                            lines.append(f"— {_author}")
        except Exception as e:
            print(f"Forismatic midday error: {e}")

        await send_with_retry(bot, chat_id, "\n".join(lines))

    except Exception as e:
        print(f"Scheduler midday briefing error: {e}")


# ─── Вечерний брифинг 21:00 (новый) ──────────────────────────────────

async def send_evening_briefing(bot, chat_id: int):
    """21:00 — вечерний брифинг + итоги дня объединённые."""
    try:
        print("Scheduler: вечерний брифинг 21:00...")
        await sync_all_tasks()
        from grok import ask_grok
        from bot.messages import _lesson_emoji, _s, _short_course

        now = datetime.datetime.now(tz=UFA_TZ)
        today = now.date()
        tomorrow = (now + datetime.timedelta(days=1)).date()
        after_tomorrow = (now + datetime.timedelta(days=2)).date()

        MONTHS = ["января","февраля","марта","апреля","мая","июня","июля","августа","сентября","октября","ноября","декабря"]
        DAYS = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]
        tom_str = f"{tomorrow.day} {MONTHS[tomorrow.month-1]}, {DAYS[tomorrow.weekday()]}"

        # Расписание завтра
        schedule_tomorrow = await _fetch_tomorrow_schedule()

        # Все задачи
        all_tasks = get_tasks()
        pending = [t for t in all_tasks if not t.get("done")]

        # Выполненные сегодня
        done_today = []
        for t in all_tasks:
            if not t.get("done"):
                continue
            done_at = t.get("done_at")
            if done_at:
                try:
                    d = datetime.datetime.fromisoformat(done_at).astimezone(UFA_TZ).date()
                    if d == today:
                        done_today.append(t)
                except Exception:
                    pass

        # Все пары сегодня
        schedule_today = await _retry(_fetch_schedule_fresh_or_cache) or []
        passed_today = schedule_today

        # Дедлайны завтра + послезавтра
        urgent = []
        for t in pending:
            if not t.get("deadline"):
                continue
            try:
                d = datetime.datetime.fromisoformat(t["deadline"]).astimezone(UFA_TZ).date()
                if d in (tomorrow, after_tomorrow):
                    days_left = (d - today).days
                    urgent.append((days_left, t))
            except Exception:
                continue
        urgent.sort(key=lambda x: x[0])

        # 2 задачи закрыть сегодня вечером (ближайшие по дедлайну, без дублей)
        _seen_titles = set()
        _close_candidates = []
        for t in sorted([t for t in pending if t.get("deadline")], key=lambda x: x["deadline"]):
            _title = t.get("title", "")
            if _title not in _seen_titles:
                _seen_titles.add(_title)
                _close_candidates.append(t)
        close_tonight = _close_candidates[:2]

        # Записываем статистику
        record_daily_stats(len(done_today), len(pending), len(passed_today))

        SEP = "┄" * 20
        border = "═" * 26
        pad = "   "
        lines = [
            f"🌙 *Добрый вечер, Ильнур!*",
            f"{now.day} {MONTHS[now.month-1]}, {DAYS[now.weekday()]}",
            "",
        ]

        # Итоги дня
        lines.append("*📊 ИТОГИ ДНЯ*")
        lines.append(SEP)
        if passed_today:
            for l in passed_today:
                name = _s(l.get("course_name")) or _s(l.get("name", ""))
                start = _s(l.get("start_time"))
                lines.append(f"🎓  {start}  {name}")
        else:
            lines.append("Пар сегодня не было")
        lines.append("")
        lines.append(f"✅ Выполнено задач: {len(done_today)}")
        lines.append(f"📋 Осталось: {len(pending)}")
        lines.append("")

        # Расписание завтра
        lines.append(f"*📅 ЗАВТРА — {tom_str.upper()}*")
        lines.append(SEP)
        if schedule_tomorrow:
            for lesson in schedule_tomorrow:
                emoji = _lesson_emoji(lesson)
                name = _s(lesson.get("course_name")) or _s(lesson.get("name"))
                start = _s(lesson.get("start_time"))
                lines.append(f"{emoji}  {start}  {name}")
        else:
            lines.append("  Пар нет 🎉")
        lines.append("")

        # Дедлайны завтра-послезавтра
        if urgent:
            lines.append("⚠️ *Дедлайны завтра-послезавтра:*")
            for days_left, t in urgent[:4]:
                label = "завтра" if days_left == 1 else "послезавтра"
                if t.get("source") == "manual":
                    course = "Личная задача"
                else:
                    course = _short_course(t.get("course_name", ""))
                lines.append(f"  🔴 {course} — {t['title'][:35]} _({label})_")
            lines.append("")

        # Закрыть сегодня вечером
        if close_tonight:
            lines.append("🎯 *Закрой сегодня вечером:*")
            for t in close_tonight:
                course = _short_course(t.get("course_name", ""))
                lines.append(f"  • {course} — {t['title'][:35]}")
            lines.append("")

        # Groq анализ
        try:
            pairs_str = ", ".join(
                (_s(l.get("course_name")) or _s(l.get("name", "")))[:20]
                for l in passed_today[:3]
            ) if passed_today else "пар не было"
            done_str = ", ".join(t["title"][:20] for t in done_today[:3]) if done_today else "ничего"
            week_summary = get_stats_summary()
            avg = get_weekly_done_avg()
            prompt = (
                f"Итог дня студента Ильнура (1 курс ИСиТ):\n"
                f"Пары сегодня: {pairs_str}\n"
                f"Выполнено задач: {len(done_today)} ({done_str})\n"
                f"Осталось: {len(pending)}\n"
                f"Среднее за неделю: {avg} задач/день\n"
                f"Статистика 7 дней:\n{week_summary}\n\n"
                f"Напиши честный короткий анализ: сравни сегодня со средним, "
                f"отметь прогресс или регресс, дай один конкретный совет на завтра. "
                f"2-3 предложения. Только текст без скобок и пояснений. Только русские слова."
            )
            analysis = await ask_grok(prompt)
            if analysis:
                lines.append(f"{'─' * 20}\n🤖 {analysis}")
        except Exception as e:
            print(f"Groq evening analysis error: {e}")

        # Анализ учёбы
        try:
            study = await _get_study_analysis_short()
            if study:
                lines.append(f"\n{'─' * 20}\n📊 *УЧЁБА*\n{study}")
        except Exception as e:
            print(f"Evening study analysis error: {e}")

        # IT новость с Hacker News
        try:
            it_news = await _fetch_it_news()
            if it_news:
                from grok import ask_grok as _ask_grok
                _result = await _ask_grok(
                    f"IT новость: {it_news}\n\n"
                    f"Напиши 1-2 предложения на русском: переведи заголовок и объясни о чём это. "
                    f"Без вступлений, только суть."
                )
                if _result:
                    lines.append(f"\n{'─' * 20}\n💻 *IT НОВОСТЬ*\n{_result}")
        except Exception as e:
            print(f"Evening IT news error: {e}")

        await send_with_retry(bot, chat_id, "\n".join(lines))
    except Exception as e:
        print(f"Scheduler evening briefing error: {e}")


# ─── Итоги дня 23:00 ─────────────────────────────────────────────────

async def send_daily_results(bot, chat_id: int):
    """23:00 — итоги дня: пары + задачи + Groq анализ + спокойной ночи последним."""
    try:
        print("Scheduler: итоги дня 23:00...")
        from grok import ask_grok
        from bot.messages import _short_course

        tasks = get_tasks()
        now = datetime.datetime.now(tz=UFA_TZ)
        today = now.date()

        # Задачи выполненные сегодня
        done_today = []
        for t in tasks:
            if not t.get("done"):
                continue
            done_at = t.get("done_at")
            if done_at:
                try:
                    d = datetime.datetime.fromisoformat(done_at).astimezone(UFA_TZ).date()
                    if d == today:
                        done_today.append(t)
                except Exception:
                    pass

        pending = [t for t in tasks if not t.get("done")]

        # Пары которые прошли сегодня
        schedule_today = await _retry(_fetch_schedule_fresh_or_cache) or []
        passed = []
        for lesson in schedule_today:
            try:
                end_dt = datetime.datetime.fromisoformat(lesson.get("end", ""))
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=UFA_TZ)
                if end_dt < now:
                    passed.append(lesson)
            except Exception:
                continue

        # Записываем статистику дня (переживает перезапуски)
        record_daily_stats(len(done_today), len(pending), len(passed))

        lines = ["🌃 *Итоги дня*\n"]

        if passed:
            lines.append("📚 *Пары сегодня:*")
            for l in passed:
                from bot.messages import _s
                name = _s(l.get("course_name")) or _s(l.get("name"))
                start = _s(l.get("start_time"))
                lines.append(f"  ✓ {start} — {name}")
            lines.append("")

        lines.append(f"✅ Выполнено задач сегодня: *{len(done_today)}*")
        if done_today:
            for t in done_today[:5]:
                course = _short_course(t.get("course_name", ""))
                lines.append(f"  • {course} — {t['title'][:40]}")
        else:
            lines.append("  _ни одной задачи не отмечено_")

        lines.append(f"📋 Осталось незакрытых: *{len(pending)}*")

        try:
            pairs_str = ", ".join(
                (l.get("course_name") or l.get("name", ""))[:20]
                for l in passed[:3]
            ) if passed else "пар не было"
            done_str = ", ".join(
                t["title"][:25] for t in done_today[:3]
            ) if done_today else "ничего не выполнено"
            week_summary = get_stats_summary()
            avg = get_weekly_done_avg()
            prompt = (
                f"Итог дня студента Ильнура:\n"
                f"Пары сегодня: {pairs_str}\n"
                f"Выполнено задач сегодня: {len(done_today)} ({done_str})\n"
                f"Осталось незакрытых: {len(pending)}\n"
                f"Среднее выполненных за неделю: {avg} задач/день\n"
                f"Статистика последних 7 дней:\n{week_summary}\n\n"
                f"Напиши честный короткий анализ: сравни сегодня со средним за неделю, "
                f"отметь прогресс или регресс, дай один конкретный совет на завтра.\n"
                f"2-3 предложения. Только сам текст без пояснений, скобок и вариантов. "
                f"Только русские слова. Перечитай и удали всё лишнее после последнего предложения."
            )
            analysis = await ask_grok(prompt)
            if analysis:
                lines.append(f"\n{'─' * 20}\n🤖 {analysis}")
        except Exception as e:
            print(f"Groq daily analysis error: {e}")
        try:
            import random as _random
            jokes4 = [
                "шутка про то что мозг продолжает учиться во сне",
                "юмор про студента который сделал всё или почти всё",
                "сарказм про завтрашние планы которые точно выполнятся",
                "шутка про подушку как лучшего друга студента",
                "мотивирующее пожелание с намёком на завтрашние задачи",
                "шутка про то что во сне мозг компилирует весь код дня",
                "юмор про студента ИТ который видит сны про рекурсию",
                "шутка про то что завтра баги будут исправлены после сна",
                "юмор про sleep который нужен и компьютеру и студенту",
                "сарказм про то что завтра точно напишу чистый код",
            ]
            prompt = (
                f"Студент Ильнур идёт спать. Прошёл {len(passed)} пар, выполнил {len(done_today)} задач.\n"
                f"Напиши смешное пожелание спокойной ночи.\n"
                f"Ровно 1-2 предложения. После последней точки — ничего. Никаких скобок, пояснений, смайлов в конце. Только русские слова."
            )
            joke = await ask_grok(prompt, system="Ты пишешь короткие острые фразы. Формат ответа: только сам текст, 1-2 предложения, точка в конце. Никаких скобок, подписей, P.S., пояснений, вариантов. Пример правильного ответа: Ильнур, дедлайн уже греет чайник — вставай и делай.")
            if joke:
                lines.append(f"😴 {_clean_joke(joke)}")
        except Exception as e:
            print(f"Groq night joke error: {e}")
        await send_with_retry(bot, chat_id, "\n".join(lines))
    except Exception as e:
        print(f"Scheduler daily results error: {e}")



# ─── Обёртки для study_theory ────────────────────────────────────────

async def send_subject_theory_job(bot, chat_id: int):
    try:
        from study_theory import send_subject_theory
        await send_subject_theory(bot, chat_id)
    except Exception as e:
        print(f"subject theory error: {e}")


async def send_english_theory_job(bot, chat_id: int):
    try:
        from study_theory import send_english_theory
        await send_english_theory(bot, chat_id)
    except Exception as e:
        print(f"english theory error: {e}")



# ─── ВК мониторинг ───────────────────────────────────────────────────

async def _fetch_it_news() -> str:
    """Получаем одну IT новость с Хабра через RSS."""
    try:
        import httpx as _httpx
        import xml.etree.ElementTree as ET
        async with _httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(
                "https://www.cnews.ru/inc/rss/news.xml",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            root = ET.fromstring(r.text)
            items = root.findall(".//item")
            import random as _random
            _random.shuffle(items)
            for item in items[:10]:
                title = item.findtext("title", "").strip()
                if title and len(title) > 15:
                    return title
    except Exception as e:
        print(f"IT news Habr error: {e}")
    return ""


async def check_vk_and_notify(bot, chat_id: int):
    """Каждые 15 минут — проверяем новые сообщения в беседе ВК за сегодня."""
    now = datetime.datetime.now(tz=UFA_TZ)
    if not (8 <= now.hour < 22):
        return
    try:
        from parsers.vk_browser import fetch_todays_vk_messages, _mark_hash_seen, _format_with_ai, _load_seen, _save_seen

        # Сброс seen_hashes в полночь — чтобы новый день начинался чисто
        now = datetime.datetime.now(tz=UFA_TZ)
        seen = _load_seen()
        last_reset = seen.get("last_reset_date", "")
        today_str = now.date().isoformat()
        if last_reset != today_str:
            seen["seen_hashes"] = []
            seen["last_reset_date"] = today_str
            _save_seen(seen)
            print("VK: seen_hashes сброшены для нового дня")

        messages = await fetch_todays_vk_messages()
        if not messages:
            return

        for msg in messages:
            try:
                vk_text = msg["text"]
                msg_hash = msg["hash"]

                # Сразу помечаем как увиденное — чтобы не отправить дважды даже при ошибке
                _mark_hash_seen(msg_hash)

                # Форматируем через AI
                formatted = await _format_with_ai(vk_text)
                if not formatted:
                    formatted = vk_text

                full_text = "💬 *ВКонтакте*\n💬 *Новое сообщение*\n" + "─" * 20 + "\n" + formatted
                if len(full_text) > 4000:
                    full_text = full_text[:4000]

                # Отправляем с отключённым превью ссылок
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=full_text,
                        parse_mode="Markdown",
                        disable_web_page_preview=True
                    )
                except Exception:
                    # Если Markdown не прошёл — отправляем без форматирования
                    await bot.send_message(
                        chat_id=chat_id,
                        text=full_text,
                        disable_web_page_preview=True
                    )

                print(f"VK: отправлено сообщение hash={msg_hash}")

            except Exception as e:
                print(f"VK: ошибка отправки сообщения: {e}")

    except Exception as e:
        print(f"VK check error: {e}")


async def send_midday_briefing_no_vk(bot, chat_id: int):
    """Дневная сводка без ВК блока — вызывается после получения ВК сообщения."""
    try:
        from grok import ask_grok
        from bot.messages import _short_course

        tasks = get_pending_tasks()
        now = datetime.datetime.now(tz=UFA_TZ)

        lines = ["🌞 *Дневная сводка*\n"]

        # Срочные сегодня + завтра (только непросроченные)
        urgent = []
        for t in tasks:
            if not t.get("deadline"):
                continue
            try:
                dt = datetime.datetime.fromisoformat(t["deadline"]).astimezone(UFA_TZ)
                days = (dt - now).days
                if 0 <= days <= 1:
                    urgent.append((days, dt, t))
            except Exception:
                continue
        urgent.sort(key=lambda x: (x[0], x[1]))

        if urgent:
            lines.append("🔴 *Горят дедлайны:*")
            seen_titles = set()
            shown = 0
            for days, dt, t in urgent:
                title = t.get("title", "")
                if title in seen_titles:
                    continue
                seen_titles.add(title)
                label = "🔴 сегодня" if days == 0 else "🟡 завтра"
                course = _short_course(t.get("course_name", ""))
                date_str = dt.strftime("%d.%m")
                lines.append(f"  ❗ *{course}* — {title[:35]} _({label}, {date_str})_")
                shown += 1
                if shown >= 5:
                    break
        else:
            lines.append("✅ Срочных дедлайнов нет")

        await send_with_retry(bot, chat_id, "\n".join(lines))

        # Шутка
        try:
            import random as _r
            jokes = [
                "шутка про дедлайны которые уже близко",
                "сарказм про студента который смотрит в потолок",
                "мотивация через страх провалить сессию",
                "юмор про то что задачи сами себя не сдадут",
                "жёсткий юмор про откладывание на потом",
            ]
            prompt = (
                f"Студент Ильнур, осталось задач: {len(tasks)}. "
                f"Стиль: {_r.choice(jokes)}. "
                f"Напиши ОДНУ оригинальную смешную фразу. "
                f"Можно лёгкий мат. 1-2 предложения. Не начинай с Чувак."
            )
            joke = await ask_grok(prompt)
            if joke:
                await send_with_retry(bot, chat_id, f"😤 {joke}")
        except Exception:
            pass

    except Exception as e:
        print(f"Midday no vk error: {e}")



async def check_lms_grades_and_notify(bot, chat_id: int):
    """Каждый час — проверяем новые оценки в LMS."""
    try:
        from parsers.lms import fetch_lms_grades_changes
        from bot.messages import format_lms_grade_notification
        from storage import get_tasks, save_tasks

        changes = await _retry(fetch_lms_grades_changes) or []

        if changes:
            for change in changes:
                sent_key = change.get("_sent_key", "")
                grade_key = f"lms_grade:{sent_key}"
                if _is_notification_sent(grade_key):
                    continue
                text = format_lms_grade_notification(change)
                sent_ok = await send_with_retry(bot, chat_id, _notify_header("🎓 Новая оценка — LMS") + text)
                if sent_ok:
                    _mark_notification_sent(grade_key)
                    # Помечаем в LMS sent после успешной отправки
                    try:
                        from parsers.lms import _load_lms_grades_sent, _save_lms_grades_sent
                        _sent = _load_lms_grades_sent()
                        _sent.add(sent_key)
                        _save_lms_grades_sent(_sent)
                    except Exception:
                        pass

    except Exception as e:
        print(f"LMS grades notify error: {e}")


MIDDAY_SENT_FILE = "data/midday_sent.json"


def _is_midday_sent() -> bool:
    """Проверяем — была ли уже дневная сводка сегодня."""
    try:
        import json as _json
        with open(MIDDAY_SENT_FILE) as f:
            data = _json.load(f)
        today = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
        return data.get("date") == today
    except Exception:
        return False


def _mark_midday_sent():
    """Помечаем что дневная сводка сегодня уже отправлена."""
    import json as _json
    os.makedirs("data", exist_ok=True)
    today = datetime.datetime.now(tz=UFA_TZ).date().isoformat()
    with open(MIDDAY_SENT_FILE, "w") as f:
        _json.dump({"date": today}, f)


async def send_goodnight(bot, chat_id: int):
    """23:00 — спокойной ночи + анекдот с anekdot.ru."""
    try:
        print("Scheduler: спокойной ночи 23:00...")
        from grok import ask_grok
        import httpx

        anekdot = ""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    "https://jokesrv.ephemera.services/",
                    headers={"Accept": "application/json"}
                )
                if r.status_code == 200:
                    data = r.json()
                    joke_en = data.get("content", "")
                    if joke_en:
                        # Переводим через ИИ
                        translated = await ask_grok(
                            f"Переведи этот анекдот на русский, сохрани юмор и стиль. "
                            f"Только перевод, без пояснений:\n{joke_en}"
                        )
                        if translated:
                            anekdot = translated
        except Exception as e:
            print(f"Goodnight: анекдот не загрузился: {e}")

        # Если анекдот не получили — генерируем через ИИ
        if not anekdot:
            try:
                anekdot = await ask_grok(
                    "Расскажи короткий смешной анекдот про студентов, программистов или учёбу. "
                    "Только сам анекдот, без предисловий. 3-5 предложений.",
                    system="Ты рассказываешь анекдоты. Только текст анекдота, ничего лишнего."
                )
            except Exception as e:
                print(f"Goodnight: ИИ анекдот не сгенерировался: {e}")

        lines = ["😴 *Спокойной ночи, Ильнур!*\n"]
        if anekdot:
            lines.append(f"{'─' * 20}")
            lines.append(f"😄 *Анекдот на ночь:*\n")
            lines.append(anekdot)
            lines.append(f"{'─' * 20}")

        await send_with_retry(bot, chat_id, "\n".join(lines))
    except Exception as e:
        print(f"Scheduler goodnight error: {e}")


def setup_scheduler(bot, chat_id: int) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(
        timezone=UFA_TZ,
        job_defaults={"misfire_grace_time": 7200}  # для фоновых джобов
    )

    # ── Утро 9:00 ──
    scheduler.add_job(send_morning_briefing, trigger="cron", hour=9, minute=0,
                      args=[bot, chat_id], id="morning_9", misfire_grace_time=60)
    # check_deadline_reminders убран — дедлайны показываются в утреннем брифинге

    # 9:30 английский #1
    scheduler.add_job(send_english_theory_job, trigger="cron", hour=9, minute=30,
                      args=[bot, chat_id], id="theory_english_930", misfire_grace_time=60)

    # 14:00 дневная сводка
    scheduler.add_job(send_midday_briefing, trigger="cron", hour=14, minute=0,
                      args=[bot, chat_id], id="midday_14", misfire_grace_time=60)

    # 14:00 теория по предмету
    scheduler.add_job(send_subject_theory_job, trigger="cron", hour=14, minute=0,
                      args=[bot, chat_id], id="theory_subject_1400", misfire_grace_time=60)

    # 15:30 английский #2
    scheduler.add_job(send_english_theory_job, trigger="cron", hour=15, minute=30,
                      args=[bot, chat_id], id="theory_english_1530", misfire_grace_time=60)

    # ── Вечер 21:00 — вечерний брифинг + итоги дня ──
    scheduler.add_job(send_evening_briefing, trigger="cron", hour=21, minute=0,
                      args=[bot, chat_id], id="evening_21", misfire_grace_time=60)

    # 21:30 английский #3
    scheduler.add_job(send_english_theory_job, trigger="cron", hour=21, minute=30,
                      args=[bot, chat_id], id="theory_english_2130", misfire_grace_time=60)

    # 23:00 спокойной ночи + анекдот
    scheduler.add_job(send_goodnight, trigger="cron", hour=23, minute=0,
                      args=[bot, chat_id], id="goodnight_23", misfire_grace_time=60)

    # ── Воскресенье 20:00 недельный отчёт ──
    scheduler.add_job(send_weekly_report, trigger="cron", day_of_week="sun", hour=20, minute=0,
                      args=[bot, chat_id], id="weekly_report", misfire_grace_time=60)

    # ВК мониторинг каждые 15 минут (8:00-22:00)
    scheduler.add_job(check_vk_and_notify, trigger="interval", minutes=15,
                      args=[bot, chat_id], id="vk_monitor")

    # ── Фоновые джобы ──
    scheduler.add_job(check_grades_and_notify, trigger="interval", minutes=10,
                      args=[bot, chat_id], id="grades_check")
    scheduler.add_job(check_lms_grades_and_notify, trigger="interval", minutes=15,
                      start_date=datetime.datetime.now(tz=UFA_TZ) + datetime.timedelta(minutes=2),
                      args=[bot, chat_id], id="lms_grades_check")
    scheduler.add_job(check_mail_and_notify, trigger="interval", minutes=5,
                      args=[bot, chat_id], id="mail_check")
    scheduler.add_job(check_messenger_and_notify, trigger="interval", minutes=5,
                      start_date=datetime.datetime.now(tz=UFA_TZ) + datetime.timedelta(minutes=2),
                      args=[bot, chat_id], id="messenger_check")
    scheduler.add_job(retry_pending_notifications, trigger="interval", minutes=10,
                      args=[bot], id="retry_notifications")
    scheduler.add_job(check_random_reminder, trigger="interval", minutes=5,
                      args=[bot, chat_id], id="random_reminder")
    scheduler.add_job(check_user_reminders, trigger="interval", minutes=5,
                      args=[bot, chat_id], id="user_reminders")
    scheduler.add_job(check_lesson_reminders, trigger="interval", minutes=5,
                      args=[bot, chat_id], id="lesson_reminders")

    print("Scheduler: настроен ✅")
    return scheduler

