import asyncio
import datetime
import json
import os
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from config import UFA_TZ, PARSE_HOURS, USER_NAME, WEATHER_LAT, WEATHER_LON
from storage import get_pending_tasks, get_tasks, save_tasks

MONTHS_RU = ["января","февраля","марта","апреля","мая","июня","июля","августа","сентября","октября","ноября","декабря"]
DAYS_RU = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]


PENDING_NOTIFICATIONS_FILE = "data/pending_notifications.json"
JARVIS_QUEUE_FILE = "data/jarvis_queue.json"

# Lock для Playwright — мессенджер и ВК не запускаются одновременно
import asyncio as _asyncio_lock
_playwright_lock = _asyncio_lock.Lock()


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
        "Выбери фильтр", "Выбери период", "ДедЛайнер запущен",
        "Привет, " + USER_NAME, "Отмечено выполненными", "Задание удалено",
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
    now_h = datetime.datetime.now(tz=UFA_TZ).hour
    if 0 <= now_h < 9:
        return
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

async def sync_all_tasks(bot=None, chat_id=None):
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

        # Перезагружаем после mark_lms_tasks_done — он мог изменить файл
        existing_tasks = get_tasks()
        existing_ids = {t.get("id") for t in existing_tasks}

        import os as _os3
        notified_file = "data/notified_tasks.json"
        try:
            notified = json.load(open(notified_file)) if _os3.path.exists(notified_file) else []
        except Exception:
            notified = []
        notified_changed = False

        updated = 0
        for t in (lms_tasks or []) + (netology_tasks or []):
            task_id = t.get("id")
            notif_key = str(task_id) if task_id else f"{t.get('title','')}_{t.get('course_name','')}"

            # Если задача уже есть — обновляем дедлайн если изменился
            found = False
            for existing in existing_tasks:
                if str(existing.get("id")) == str(task_id):
                    found = True
                    if existing.get("deadline") != t.get("deadline") and t.get("deadline"):
                        existing["deadline"] = t["deadline"]
                        updated += 1
                        print(f"Scheduler: обновлён дедлайн: {t.get('title','')[:40]}")
                    break
            if not found:
                key = (t.get("title", ""), t.get("course_name", ""))
                existing_key_pairs = {(e.get("title",""), e.get("course_name","")) for e in existing_tasks}
                if key not in existing_key_pairs:
                    existing_tasks.append(t)
                    existing_ids.add(task_id)
                    added += 1

            # Уведомление — отдельно от добавления, по notified_key
            if bot and chat_id and t.get("source") in ("lms", "netology"):
                if notif_key not in notified:
                    # Тихий старт — первые 20 минут только помечаем, не шлём
                    grace_file = "data/startup_grace.json"
                    in_grace = False
                    try:
                        if _os3.path.exists(grace_file):
                            import time as _time
                            grace_data = json.load(open(grace_file))
                            if _time.time() - grace_data.get("started_at", 0) < 1200:
                                in_grace = True
                    except Exception:
                        pass
                    notified.append(notif_key)
                    notified_changed = True
                    if in_grace:
                        continue
                    source_name = "LMS" if t.get("source") == "lms" else "Нетология"
                    title = t.get("title", "Без названия")
                    course = t.get("course_name", "")
                    deadline = t.get("deadline", "")
                    deadline_str = ""
                    if deadline:
                        try:
                            dt = datetime.datetime.fromisoformat(deadline).astimezone(UFA_TZ)
                            deadline_str = f"\n📅 Дедлайн: {dt.strftime('%d.%m.%Y %H:%M')}"
                        except Exception:
                            pass
                    text = (
                        f"💬 *{source_name}*\n"
                        "\n"
                        "💬 Новая задача\n"
                        "────────────────────\n"
                        f"📌 {title}\n"
                        f"📚 {course}"
                        f"{deadline_str}"
                    )
                    try:
                        await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
                    except Exception as ex:
                        print(f"sync_all_tasks: не удалось отправить уведомление: {ex}")

        if notified_changed:
            try:
                with open(notified_file, "w") as _f:
                    json.dump(notified[-500:], _f, ensure_ascii=False)
                print(f"sync_all_tasks: сохранено {len(notified)} уведомлённых задач")
            except Exception as e:
                print(f"sync_all_tasks: ошибка сохранения notified: {e}")

        if updated:
            print(f"Scheduler: обновлено дедлайнов: {updated}")

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
            f"Данные успеваемости студента {USER_NAME}:\n{raw}\n\n"
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
                lms_tasks, completed_ids = lms_result
            else:
                lms_tasks, completed_ids = [], set()
            from storage import mark_lms_tasks_done
            marked = mark_lms_tasks_done(completed_ids, lms_tasks)
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
            text = new_email_message(email_data)
            keyboard = task_from_message_keyboard(email_data["id"])
            sent = await send_with_retry(bot, chat_id, text, parse_mode="HTML", reply_markup=keyboard)
            if sent:
                add_seen_message(email_data["id"])

    except Exception as e:
        print(f"Scheduler mail check error: {e}")


async def check_messenger_and_notify(bot, chat_id: int):
    if _playwright_lock.locked():
        print("Messenger: Playwright занят — пропускаем")
        return
    async with _playwright_lock:
        await _check_messenger_and_notify_inner(bot, chat_id)

async def _check_messenger_and_notify_inner(bot, chat_id: int):
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
            text = new_messenger_message(msg)
            keyboard = task_from_message_keyboard(msg["id"])
            sent = await send_with_retry(bot, chat_id, text, parse_mode="HTML", reply_markup=keyboard)
            if sent:
                add_seen_message(msg["id"])

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

        schedule = await asyncio.wait_for(fetch_schedule_today(), timeout=15)
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
                f"Студент {USER_NAME}, самая срочная задача: {course} — {title}, через {min_days} дн. "
                f"Напиши одну короткую мотивирующую фразу про эту задачу. "
                f"Без предисловий, без скобок. Только сама фраза."
            )
            ai_line = await ask_grok(prompt)

        # Цитата из файла
        quote_text = _get_quote()
        quote_author = ""

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
        week_summary = get_stats_summary()
        avg = get_weekly_done_avg()

        # Просроченные
        now = datetime.datetime.now(tz=UFA_TZ)
        overdue = [t for t in tasks if not t.get("done") and t.get("deadline") and
            (datetime.datetime.fromisoformat(t["deadline"]).astimezone(UFA_TZ) - now).days < 0]

        lines = [
            "📊 *Недельный отчёт*",
            "",
            f"✅ Выполнено: *{done}*",
            f"📋 Осталось: *{pending}*",
            f"📈 Выполнение: *{pct}%*",
            f"📉 Среднее в день: *{avg}*",
            "",
        ]
        if week_summary and week_summary != "нет данных":
            lines.append("*📅 По дням:*")
            lines.append(week_summary)
            lines.append("")
        if overdue:
            lines.append(f"⚠️ Просроченных задач: *{len(overdue)}*")
            for t in overdue[:3]:
                course = t.get("course_name", "")[:25]
                lines.append(f"  • {course} — {t.get('title','')[:40]}")
            lines.append("")

        await send_with_retry(bot, chat_id, "\n".join(lines))

        prompt = (
            f"Итог недели студента {USER_NAME}: выполнено {done} из {done+pending} задач ({pct}%). "
            f"Среднее в день: {avg}. Просроченных: {len(overdue)}. "
            f"Статистика по дням:\n{week_summary}\n\n"
            f"Напиши 3 предложения: оцени неделю честно, отметь тенденцию, дай конкретный совет на следующую. "
            f"Без воды, по-русски."
        )
        grok_text = await ask_grok(prompt)
        if grok_text:
            await send_with_retry(bot, chat_id, f"🤖 {grok_text}")

    except Exception as e:
        print(f"Scheduler weekly report error: {e}")


# ─── Главные задачи планировщика ─────────────────────────────────────


# ─── Настройка планировщика ───────────────────────────────────────────


# ─── Утренний брифинг 9:00 (новый) ───────────────────────────────────


async def _fetch_weather() -> str:
    """Погода через Open-Meteo (без ключа)."""
    try:
        import httpx
        lat, lon = WEATHER_LAT, WEATHER_LON
        WMO = {
            0:"ясно",1:"почти ясно",2:"переменная облачность",3:"пасмурно",
            45:"туман",48:"туман с инеем",51:"лёгкая морось",53:"морось",55:"сильная морось",
            61:"лёгкий дождь",63:"дождь",65:"сильный дождь",
            71:"лёгкий снег",73:"снег",75:"сильный снег",77:"снежная крупа",
            80:"ливень",81:"ливни",82:"сильный ливень",
            85:"снегопад",86:"сильный снегопад",
            95:"гроза",96:"гроза с градом",99:"гроза с сильным градом",
        }
        def _icon(code):
            if code == 0: return "☀️"
            elif code in (1,2): return "⛅"
            elif code == 3: return "☁️"
            elif code in (45,48): return "🌫️"
            elif code in (51,53,55,61,63,65,80,81,82): return "🌧️"
            elif code in (71,73,75,77,85,86): return "❄️"
            elif code in (95,96,99): return "⛈️"
            else: return "🌤️"

        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat, "longitude": lon,
                    "current": "temperature_2m,weathercode,windspeed_10m,precipitation",
                    "hourly": "temperature_2m,weathercode,windspeed_10m",
                    "forecast_days": 1,
                    "timezone": "auto",
                }
            )
            d = r.json()

        cur = d["current"]
        hourly = d["hourly"]
        now_temp = round(cur["temperature_2m"])
        now_code = cur["weathercode"]
        now_wind = round(cur["windspeed_10m"])
        now_desc = WMO.get(now_code, "")
        now_precip = cur.get("precipitation", 0)
        precip_str = f" 🌧️{now_precip:.1f}мм" if now_precip and now_precip > 0 else ""

        times = hourly["time"]
        temps = hourly["temperature_2m"]
        codes = hourly["weathercode"]
        winds = hourly["windspeed_10m"]

        def _get_hour(target_h):
            for i, t in enumerate(times):
                h = int(t[11:13])
                if h >= target_h:
                    return temps[i], codes[i], winds[i]
            return None, None, None

        out = []
        out.append(f"{_icon(now_code)} Сейчас {now_temp}°C, {now_desc}, ветер {now_wind} м/с{precip_str}")

        d_temp, d_code, d_wind = _get_hour(13)
        if d_temp is not None:
            out.append(f"{_icon(d_code)} Днём {round(d_temp)}°C, {WMO.get(d_code,'')}, ветер {round(d_wind)} м/с")

        e_temp, e_code, e_wind = _get_hour(19)
        if e_temp is not None:
            out.append(f"{_icon(e_code)} Вечером {round(e_temp)}°C, {WMO.get(e_code,'')}, ветер {round(e_wind)} м/с")

        return "\n".join(out)

    except Exception as e:
        print(f"Weather fetch error: {e}")
        return ""
MORNING_SENT_FILE = "data/morning_sent.json"



def _is_evening_sent() -> bool:
    try:
        import json
        from config import UFA_TZ
        import datetime
        with open(EVENING_SENT_FILE) as f:
            data = json.load(f)
        return data.get("date") == datetime.datetime.now(tz=UFA_TZ).date().isoformat()
    except Exception:
        return False

def _mark_evening_sent():
    import json, os, datetime
    from config import UFA_TZ
    os.makedirs("data", exist_ok=True)
    with open(EVENING_SENT_FILE, "w") as f:
        json.dump({"date": datetime.datetime.now(tz=UFA_TZ).date().isoformat()}, f)

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
        await sync_all_tasks()
        from grok import ask_grok
        from bot.messages import _expand_and_sort, _lesson_emoji, _s, _short_course
        schedule = await _retry(_fetch_schedule_fresh_or_cache) or []
        tasks = get_pending_tasks()
        now = datetime.datetime.now(tz=UFA_TZ)
        SEP = "┄" * 20
        day_str = f"{DAYS_RU[now.weekday()].capitalize()}, {now.day} {MONTHS_RU[now.month-1]}"
        border = "═" * 26
        pad = "   "
        lines = [
            f"☀️ *Доброе утро, {USER_NAME}!*",
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
                if days <= 3:
                    urgent.append((days, t))
            except Exception:
                continue
        urgent.sort(key=lambda x: x[0])

        overdue = [(d, t) for d, t in urgent if d < 0]
        upcoming = [(d, t) for d, t in urgent if d >= 0]

        if upcoming:
            has_today = any(d == 0 for d, _ in upcoming)
            has_tomorrow = any(d == 1 for d, _ in upcoming)
            has_3days = any(d > 1 for d, _ in upcoming)
            if has_today:
                dl_label = "СЕГОДНЯ"
            elif has_tomorrow:
                dl_label = "ЗАВТРА"
            else:
                dl_label = "БЛИЖАЙШИЕ"
            lines.append(f"*🔴 ДЕДЛАЙНЫ — {dl_label}*")
            lines.append(SEP)
            seen_titles = set()
            shown = 0
            DAYS_SHORT = ["пн","вт","ср","чт","пт","сб","вс"]
            for days, t in upcoming:
                title = t.get("title", "")
                if title in seen_titles:
                    continue
                seen_titles.add(title)
                try:
                    dt = datetime.datetime.fromisoformat(t["deadline"]).astimezone(UFA_TZ)
                    date_str = f"{dt.strftime('%d.%m')} {DAYS_SHORT[dt.weekday()]}"
                except Exception:
                    date_str = ""
                course = _short_course(t.get("course_name", ""))
                # Обрезаем длинные названия
                if len(title) > 40:
                    title = title[:37] + "…"
                prefix = f"{course} — " if course else ""
                lines.append(f"❗️  {date_str}  —  {prefix}{title}")
                shown += 1
                if shown >= 5:
                    break
        elif tasks:
            lines.append(f"*📚 ЗАДАНИЯ*")
            lines.append(SEP)
            lines.append(f"Всего: {len(tasks)}, срочных нет ✅")
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

        if overdue:
            lines.append("")
            lines.append(f"⚠️ Просрочено: {len(overdue)}")
        # Цитата из файла
        _quote = _get_quote()
        if _quote:
            lines.append(f"\n{'─' * 20}\n💬 {_quote}")

        await send_with_retry(bot, chat_id, "\n".join(lines))
        _mark_morning_sent()
    except Exception as e:
        print(f"Scheduler morning briefing error: {e}")
async def send_midday_briefing(bot, chat_id: int):
    """14:00 — дневная сводка."""
    if _is_midday_sent():
        print("Scheduler: дневной брифинг уже был сегодня — пропускаем")
        return
    try:
        print("Scheduler: дневной брифинг 14:00...")
        await sync_all_tasks()
        from grok import ask_grok
        from bot.messages import _lesson_emoji, _s, _short_course

        tasks = get_pending_tasks()
        now = datetime.datetime.now(tz=UFA_TZ)
        SEP = "┄" * 20
        border = "═" * 26
        pad = "   "

        lines = [
            f"🌞 *Добрый день, {USER_NAME}!*",
            f"{DAYS_RU[now.weekday()].capitalize()}, {now.day} {MONTHS_RU[now.month-1]}",
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

        # Ближайшая задача + мотивация от ИИ (только будущие, не просрочка)
        nearest = sorted(
            [t for t in tasks if t.get("deadline") and not t.get("done")
             and datetime.datetime.fromisoformat(t["deadline"]).astimezone(UFA_TZ) >= now],
            key=lambda x: x["deadline"]
        )
        if nearest:
            t = nearest[0]
            from bot.messages import _short_course
            course = _short_course(t.get("course_name", ""))
            title = t.get("title", "")
            if len(title) > 40:
                title = title[:37] + "…"
            _days_short = ["пн","вт","ср","чт","пт","сб","вс"]
            try:
                dt = datetime.datetime.fromisoformat(t["deadline"]).astimezone(UFA_TZ)
                days = (dt - now).days
                if days == 0:
                    when = "сегодня"
                elif days == 1:
                    when = "завтра"
                else:
                    when = f"через {days} дн."
                date_str = f"{dt.strftime('%d.%m')} {_days_short[dt.weekday()]}"
            except Exception:
                when = ""
                date_str = ""
            lines.append(f"*📌 ЗАДАЧА ДНЯ*")
            lines.append(SEP)
            lines.append(f"❗️  {date_str}  —  {course} — {title}")
            lines.append("")
            lines.append("")
            try:
                prompt = (
                    f"Задача студента {USER_NAME}: {course} — {title}, дедлайн {when}. "
                    f"Напиши 2 предложения — короткую мотивацию закрыть именно эту задачу сегодня. "
                    f"Конкретно, без воды, по-русски. Можно с юмором."
                )
                motivation = await ask_grok(prompt)
                if motivation:
                    lines.append(f"💪 {motivation}")
                    lines.append("")
            except Exception as e:
                print(f"Midday motivation error: {e}")
        else:
            lines.append("✅ Активных задач нет")
            lines.append("")

        # Цитата из файла
        _quote = _get_quote()
        if _quote:
            lines.append(f"{'─' * 20}")
            lines.append(f"💬 {_quote}")

        await send_with_retry(bot, chat_id, "\n".join(lines))
        _mark_midday_sent()

    except Exception as e:
        print(f"Scheduler midday briefing error: {e}")


# ─── Вечерний брифинг 21:00 (новый) ──────────────────────────────────

async def send_evening_briefing(bot, chat_id: int):
    """21:00 — вечерний брифинг + итоги дня объединённые."""
    if _is_evening_sent():
        print("Scheduler: вечерний брифинг уже был сегодня — пропускаем")
        return
    try:
        print("Scheduler: вечерний брифинг 21:00...")
        await sync_all_tasks()
        from grok import ask_grok
        from bot.messages import _lesson_emoji, _s, _short_course

        now = datetime.datetime.now(tz=UFA_TZ)
        today = now.date()
        tomorrow = (now + datetime.timedelta(days=1)).date()
        after_tomorrow = (now + datetime.timedelta(days=2)).date()
        tom_str = f"{tomorrow.day} {MONTHS_RU[tomorrow.month-1]}, {DAYS_RU[tomorrow.weekday()]}"

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

        # 3 ближайших актуальных задачи (только days >= 0)
        _seen_titles = set()
        upcoming_tasks = []
        for t in sorted([t for t in pending if t.get("deadline")], key=lambda x: x["deadline"]):
            try:
                d = datetime.datetime.fromisoformat(t["deadline"]).astimezone(UFA_TZ)
                days_left = (d - now).days
                if days_left < 0:
                    continue
                _title = t.get("title", "")
                if _title not in _seen_titles:
                    _seen_titles.add(_title)
                    upcoming_tasks.append((days_left, t))
            except Exception:
                continue

        # Записываем статистику
        record_daily_stats(len(done_today), len(pending), len(passed_today))

        SEP = "┄" * 20
        border = "═" * 26
        pad = "   "
        lines = [
            f"🌙 *Добрый вечер, {USER_NAME}!*",
            f"{now.day} {MONTHS_RU[now.month-1]}, {DAYS_RU[now.weekday()]}",
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

        _days_short = ["пн","вт","ср","чт","пт","сб","вс"]
        # Закрой до сна — самая ближайшая задача
        if upcoming_tasks:
            days_left, t = upcoming_tasks[0]
            from bot.messages import _short_course
            course = _short_course(t.get("course_name", ""))
            title = t.get("title", "")
            if len(title) > 40:
                title = title[:37] + "…"
            try:
                dt = datetime.datetime.fromisoformat(t["deadline"]).astimezone(UFA_TZ)
                date_str = f"{dt.strftime('%d.%m')} {_days_short[dt.weekday()]}"
            except Exception:
                date_str = ""
            prefix = f"{course} — " if course else ""
            lines.append("🎯 *Закрой до сна:*")
            lines.append(f"  ❗️  {date_str}  —  {prefix}{title}")
            lines.append("")

        # 3 ближайших задачи
        if len(upcoming_tasks) > 1:
            lines.append("📌 *Ближайшие задачи:*")
            for days_left, t in upcoming_tasks[1:4]:
                from bot.messages import _short_course
                course = _short_course(t.get("course_name", ""))
                title = t.get("title", "")
                if len(title) > 40:
                    title = title[:37] + "…"
                try:
                    dt = datetime.datetime.fromisoformat(t["deadline"]).astimezone(UFA_TZ)
                    date_str = f"{dt.strftime('%d.%m')} {_days_short[dt.weekday()]}"
                except Exception:
                    date_str = ""
                prefix = f"{course} — " if course else ""
                lines.append(f"  • ❗️  {date_str}  —  {prefix}{title}")
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
                f"Итог дня студента {USER_NAME} (1 курс ИСиТ):\n"
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

        # Анализ учёбы — развёрнутый, каждый раз разный
        try:
            from parsers.study_analysis import fetch_study_analysis
            from grok import ask_grok as _ask_grok2
            raw = await fetch_study_analysis()
            if raw:
                import random as _random
                _angles = [
                    "Сосредоточься на прогрессе: что улучшилось, что просело, дай конкретный совет на завтра.",
                    "Сравни предметы между собой: где риск не закрыть, где всё хорошо, что срочно доделать.",
                    "Оцени нагрузку: какой предмет требует больше всего внимания прямо сейчас и почему.",
                    "Посмотри на посещаемость и баллы вместе: где пропуски влияют на оценку, что критично.",
                    "Дай честную оценку недели: что радует, что тревожит, один конкретный шаг на завтра.",
                ]
                angle = _random.choice(_angles)
                study_short = await _ask_grok2(
                    f"Данные успеваемости студента {USER_NAME} (1 курс):\n{raw}\n\n"
                    f"{angle}\n"
                    f"3-4 предложения, конкретно, без воды, на русском. Каждый раз свежий взгляд.",
                    system="Отвечай 3-4 предложениями на русском. Будь конкретным и честным."
                )
                if study_short:
                    lines.append(f"\n{'─' * 20}\n📊 *УЧЁБА*\n{study_short}")
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
        _mark_evening_sent()
    except Exception as e:
        print(f"Scheduler evening briefing error: {e}")


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
                    try:
                        from parsers.lms import _load_lms_grades_sent, _save_lms_grades_sent
                        _sent = _load_lms_grades_sent()
                        _sent.add(sent_key)
                        _save_lms_grades_sent(_sent)
                    except Exception:
                        pass

    except Exception as e:
        print(f"LMS grades notify error: {e}")


async def check_vk_and_notify(bot, chat_id: int):
    """Каждые 15 минут — проверяем новые сообщения в беседе ВК за сегодня."""
    now = datetime.datetime.now(tz=UFA_TZ)
    if not (8 <= now.hour < 22):
        return
    if _playwright_lock.locked():
        print("VK: Playwright занят — пропускаем")
        return
    async with _playwright_lock:
        await _check_vk_and_notify_inner(bot, chat_id)

async def _check_vk_and_notify_inner(bot, chat_id: int):
    now = datetime.datetime.now(tz=UFA_TZ)
    try:
        from parsers.vk_browser import fetch_todays_vk_messages, _mark_hash_seen, _format_with_ai

        # Хеши храним 3 дня — защита от дублей после перезапуска
        # Сброс не делаем, просто ограничиваем размер в _mark_hash_seen

        messages = await fetch_todays_vk_messages()
        if not messages:
            return

        for msg in messages:
            try:
                vk_text = msg["text"]
                msg_hash = msg["hash"]

                # Форматируем через AI
                formatted = await _format_with_ai(vk_text)
                if not formatted:
                    formatted = vk_text

                header = "<b>💬 ВКонтакте</b>\n\n<b>💬 Новое сообщение</b>\n" + "─" * 20
                full_text = f"{header}\n\n{formatted}"
                if len(full_text) > 4000:
                    full_text = full_text[:4000]

                # Отправляем в HTML
                sent_ok = False
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=full_text,
                        parse_mode="HTML",
                        disable_web_page_preview=True
                    )
                    sent_ok = True
                except Exception:
                    # Если HTML не прошёл — отправляем без форматирования
                    try:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=full_text,
                            disable_web_page_preview=True
                        )
                        sent_ok = True
                    except Exception:
                        pass

                if sent_ok:
                    _mark_hash_seen(msg_hash)
                    print(f"VK: отправлено сообщение hash={msg_hash}")
                else:
                    print(f"VK: не удалось отправить hash={msg_hash}")

            except Exception as e:
                print(f"VK: ошибка отправки сообщения: {e}")

    except Exception as e:
        print(f"VK check error: {e}")


MIDDAY_SENT_FILE = "data/midday_sent.json"
EVENING_SENT_FILE = "data/evening_sent.json"


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


def _get_quote() -> str:
    """Берём случайную цитату из файла, каждая появляется раз за полный цикл."""
    import json as _json2, os as _os2, random as _random2
    quotes_file = "data/quotes.json"
    state_file = "data/quotes_state.json"
    try:
        with open(quotes_file, encoding="utf-8") as f:
            all_quotes = _json2.load(f)
    except Exception:
        return "Успех — это сумма небольших усилий, повторяемых день за днём."
    try:
        with open(state_file) as f:
            state = _json2.load(f)
    except Exception:
        state = {"remaining": []}
    remaining = state.get("remaining", [])
    if not remaining:
        remaining = list(range(len(all_quotes)))
        _random2.shuffle(remaining)
    idx = remaining.pop(0)
    _os2.makedirs("data", exist_ok=True)
    with open(state_file, "w") as f:
        _json2.dump({"remaining": remaining}, f)
    return all_quotes[idx]


async def send_quote(bot, chat_id: int):
    """13:00 — мотивационная цитата дня."""
    import datetime as _dt
    today = _dt.datetime.now(tz=UFA_TZ).date().isoformat()
    state_file = "data/quote_sent.json"
    try:
        with open(state_file) as f:
            if json.load(f).get("date") == today:
                print("Scheduler: цитата уже была сегодня")
                return
    except Exception:
        pass
    try:
        quote = _get_quote()
        msg = f"💬 *Цитата дня*\n\n_{quote}_"
        await send_with_retry(bot, chat_id, msg, parse_mode="Markdown")
        os.makedirs("data", exist_ok=True)
        with open(state_file, "w") as f:
            json.dump({"date": today}, f)
        print("Scheduler: цитата дня отправлена")
    except Exception as e:
        print(f"Scheduler quote error: {e}")


# ─── Напоминалка 17:00 ───────────────────────────────────────────────

_REMINDERS_17 = [
    "🔥 Эй, ещё не вечер! Закрой хотя бы одну задачу до брифинга.",
    "⏰ 17:00 — самое время сделать то, что откладывал с утра.",
    "📚 Через 4 часа вечерний брифинг. Есть что отметить выполненным?",
    "🎯 Одна задача сейчас = спокойный вечер. Давай.",
    "⚡️ Рабочее время ещё идёт. Не трать его на мемы.",
    "📌 Напоминаю: дедлайны сами себя не выполнят.",
    "🚀 До конца дня 4 часа. Используй хотя бы один.",
    "😤 Ты ещё не сделал то что планировал утром. Пора.",
    "💡 Сейчас самый продуктивный момент дня. Не пропусти.",
    "🏃 Финишная прямая дня — осталось немного. Закрой одну задачу.",
    "😴 Не засыпай! До вечера ещё куча времени.",
    "🎓 Будущий ты скажет спасибо если сделаешь это сейчас.",
    "📝 5 минут чтобы начать. Начни.",
    "🤔 Что важнее — очередной ролик или закрытый дедлайн?",
    "🏆 Чемпионы не ждут вдохновения. Они просто делают.",
    "📅 Завтра будет легче если сделать сегодня.",
    "🔔 Дедлайн не спит. А ты?",
    "💪 Одно задание. Прямо сейчас. Го.",
    "🎯 Фокус! Телефон в сторону, задача перед тобой.",
    "⏳ Время идёт в любом случае. Пусть идёт с пользой.",
    "🧠 Мозг разогрет с утра. Используй пока не остыл.",
    "😎 Сделай сейчас — вечером будешь собой гордиться.",
    "🚨 Внимание: обнаружены незакрытые задачи. Требуется вмешательство.",
    "📖 Открой задание. Просто открой. Дальше само пойдёт.",
    "🌅 День ещё не закончился. Сделай его продуктивным.",
    "💥 Взрыв продуктивности через 3... 2... 1... Давай!",
    "🤖 ДедЛайнер напоминает: ты ещё не сделал домашку.",
    "🎪 Шоу называется \"Я точно сделаю это потом\". Занавес пора закрывать.",
    "😏 Дедлайн смотрит на тебя. Что скажешь?",
    "⚡️ Зарядка кончается? Нет — это продуктивность. Подзарядись делом.",
]

async def send_afternoon_reminder(bot, chat_id: int):
    """17:00 — случайная напоминалка + 2 ближайших задачи."""
    import datetime as _dt
    today = _dt.datetime.now(tz=UFA_TZ).date().isoformat()
    state_file = "data/reminder17_sent.json"
    try:
        with open(state_file) as f:
            if json.load(f).get("date") == today:
                print("Scheduler: напоминалка 17:00 уже была сегодня")
                return
    except Exception:
        pass
    try:
        import random
        msg = random.choice(_REMINDERS_17)

        # Добавляем 2 ближайших задачи
        tasks = get_pending_tasks()
        now = datetime.datetime.now(tz=UFA_TZ)
        with_deadline = []
        for t in tasks:
            try:
                d = datetime.datetime.fromisoformat(t["deadline"]).astimezone(UFA_TZ)
                days = (d - now).days
                with_deadline.append((days, t))
            except Exception:
                continue
        with_deadline.sort(key=lambda x: x[0])

        upcoming = [(d, t) for d, t in with_deadline if d >= 0]
        if upcoming:
            _days_short = ["пн","вт","ср","чт","пт","сб","вс"]
            deadline_lines = "───────────────────\n📚 *БЛИЖАЙШИЕ ДЕДЛАЙНЫ*"
            for days, t in upcoming[:3]:
                title = t.get("title", "")
                if len(title) > 40:
                    title = title[:37] + "…"
                from bot.messages import _short_course
                course = _short_course(t.get("course_name", ""))
                try:
                    dt = datetime.datetime.fromisoformat(t["deadline"]).astimezone(UFA_TZ)
                    date_str = f"{dt.strftime('%d.%m')} {_days_short[dt.weekday()]}"
                except Exception:
                    date_str = ""
                prefix = f"{course} — " if course else ""
                deadline_lines += f"\n❗️  {date_str}  —  {prefix}{title}"
            deadline_lines += "\n───────────────────"
            msg = deadline_lines + "\n\n" + msg

        await send_with_retry(bot, chat_id, msg, parse_mode="Markdown")
        os.makedirs("data", exist_ok=True)
        with open(state_file, "w") as f:
            json.dump({"date": today}, f)
        print("Scheduler: напоминалка 17:00 отправлена")
    except Exception as e:
        print(f"Scheduler reminder 17 error: {e}")

def setup_scheduler(bot, chat_id: int) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(
        timezone=UFA_TZ,
        job_defaults={"misfire_grace_time": 7200}  # для фоновых джобов
    )

    # ── Утро 9:00 ──
    scheduler.add_job(send_morning_briefing, trigger="cron", hour=9, minute=0,
                      args=[bot, chat_id], id="morning_9", misfire_grace_time=3600)
    # check_deadline_reminders убран — дедлайны показываются в утреннем брифинге

    # 9:30 английский #1
    scheduler.add_job(send_english_theory_job, trigger="cron", hour=9, minute=30,
                      args=[bot, chat_id], id="theory_english_930", misfire_grace_time=3600)

    # 14:00 дневная сводка
    scheduler.add_job(send_midday_briefing, trigger="cron", hour=14, minute=0,
                      args=[bot, chat_id], id="midday_14", misfire_grace_time=3600)

    # 11:00 теория по предмету
    scheduler.add_job(send_subject_theory_job, trigger="cron", hour=11, minute=0,
                      args=[bot, chat_id], id="theory_subject_1100", misfire_grace_time=3600)

    # 15:30 английский #2
    scheduler.add_job(send_english_theory_job, trigger="cron", hour=15, minute=30,
                      args=[bot, chat_id], id="theory_english_1530", misfire_grace_time=3600)

    # ── Вечер 21:00 — вечерний брифинг + итоги дня ──
    scheduler.add_job(send_evening_briefing, trigger="cron", hour=21, minute=0,
                      args=[bot, chat_id], id="evening_21", misfire_grace_time=3600)

    # 21:30 английский #3
    scheduler.add_job(send_english_theory_job, trigger="cron", hour=21, minute=30,
                      args=[bot, chat_id], id="theory_english_2130", misfire_grace_time=3600)


    # 13:00 цитата дня
    scheduler.add_job(send_quote, trigger="cron", hour=13, minute=0,
                      args=[bot, chat_id], id="quote_13", misfire_grace_time=3600)

    # 17:00 напоминалка
    scheduler.add_job(send_afternoon_reminder, trigger="cron", hour=17, minute=0,
                      args=[bot, chat_id], id="reminder_17", misfire_grace_time=3600)

    # ── Воскресенье 20:00 недельный отчёт ──
    scheduler.add_job(send_weekly_report, trigger="cron", day_of_week="sun", hour=20, minute=0,
                      args=[bot, chat_id], id="weekly_report", misfire_grace_time=3600)

    # ВК мониторинг каждые 15 минут (8:00-22:00)
    scheduler.add_job(check_vk_and_notify, trigger="interval", minutes=15,
                      args=[bot, chat_id], id="vk_monitor", max_instances=1, coalesce=True)

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
                      args=[bot, chat_id], id="messenger_check", max_instances=1, coalesce=True)
    scheduler.add_job(retry_pending_notifications, trigger="interval", minutes=10,
                      args=[bot], id="retry_notifications")
    scheduler.add_job(check_random_reminder, trigger="interval", minutes=5,
                      args=[bot, chat_id], id="random_reminder")
    scheduler.add_job(check_user_reminders, trigger="interval", minutes=5,
                      args=[bot, chat_id], id="user_reminders")
    scheduler.add_job(sync_all_tasks, trigger="interval", minutes=30,
                      args=[bot, chat_id], id="sync_tasks", max_instances=1, coalesce=True,
                      start_date=datetime.datetime.now(tz=UFA_TZ) + datetime.timedelta(minutes=5))
    scheduler.add_job(check_lesson_reminders, trigger="interval", minutes=5,
                      args=[bot, chat_id], id="lesson_reminders")

    print("Scheduler: настроен ✅")
    return scheduler

