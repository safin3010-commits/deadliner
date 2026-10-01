import re
import asyncio
import datetime
import httpx
from config import NETOLOGY_EMAIL, NETOLOGY_PASSWORD, UFA_TZ


class NetologyAuthError(Exception):
    """Не удалось авторизоваться в Нетологии (неверный логин/пароль, cookie
    сессии не пришла) — отличаем от "заданий/уведомлений просто нет", чтобы
    вызывающий код (sync_all_tasks) не показывал молчание вместо реальной
    ошибки (см. ModeusAuthError в parsers/modeus.py — тот же принцип)."""
    pass

NETOLOGY_BASE_URL = "https://netology.ru"
# API отдаёт модули сразу по всем семестрам (1/2/3) — уже пройденные задания
# отсеиваются флагом passed, но непройденные (например "не сдал в срок") так
# и висят в старом семестре вечно и лезли бы в tasks.json при каждой
# синхронизации. Текущий семестр — единственный источник актуальных дедлайнов.
CURRENT_SEMESTER_PREFIX = "3 семестр:"
NETOLOGY_SIGN_IN_URL = f"{NETOLOGY_BASE_URL}/backend/api/user/sign_in"
NETOLOGY_COURSES_URL = f"{NETOLOGY_BASE_URL}/backend/api/user/programs/calendar/filters"
NETOLOGY_PROGRAMS_URL = f"{NETOLOGY_BASE_URL}/backend/api/user/professions/{{calendar_id}}/schedule"
NETOLOGY_EVENTS_URL = f"{NETOLOGY_BASE_URL}/backend/api/user/programs/{{program_id}}/schedule"
NETOLOGY_UNREAD_NOTIFICATIONS_URL = f"{NETOLOGY_BASE_URL}/backend/api/user/notifications/unread_messages"
NETOLOGY_ALL_NOTIFICATIONS_URL = f"{NETOLOGY_BASE_URL}/backend/api/user/notifications/messages"

# Ищем дату в названии: "дедлайн 25.03.26", "дедлайн 30.12.2025", "до 11.01.26",
# "Дедлайн — 09.09.2026" (Нетология с 2026 использует тире-разделитель)
_DEADLINE_RE = re.compile(
    r"(?:рекомендованный\s+)?(?:дедлайн|до)\s*[—\-:]?\s*(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})",
    re.IGNORECASE
)


def _parse_deadline_from_title(title: str) -> datetime.datetime | None:
    """Извлекаем дедлайн из названия задания."""
    m = _DEADLINE_RE.search(title)
    if not m:
        return None
    try:
        day, month, year = m.group(1), m.group(2), m.group(3)
        if len(year) == 2:
            year = "20" + year
        dt = datetime.datetime(int(year), int(month), int(day), 23, 59, tzinfo=UFA_TZ)
        return dt
    except Exception:
        return None


async def auth_netology() -> str | None:
    try:
        async with httpx.AsyncClient(
            base_url=NETOLOGY_BASE_URL, timeout=20, follow_redirects=True
        ) as s:
            r = await s.post(NETOLOGY_SIGN_IN_URL, json={
                "login": NETOLOGY_EMAIL,
                "password": NETOLOGY_PASSWORD,
                "remember": True,
            })
            if r.status_code == 401:
                print("Netology: неверный логин или пароль")
                raise NetologyAuthError("неверный логин или пароль")
            r.raise_for_status()
            cookie = s.cookies.get("_netology-on-rails_session")
            if not cookie:
                print("Netology: cookie не найден")
                raise NetologyAuthError("сессия авторизовалась, но cookie не пришла")
            print("Netology: авторизация успешна ✅")
            return cookie
    except NetologyAuthError:
        raise
    except Exception as e:
        # Сетевая/временная ошибка — НЕ авторизационная, вызывающий код как и
        # раньше получает None и деградирует до "данных нет в этот раз".
        print(f"Netology auth failed: {e!r}")
        return None


async def _get(s: httpx.AsyncClient, url: str, params: dict | None = None) -> dict | list | None:
    try:
        r = await s.get(url, params=params)
        if r.status_code == 401:
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"Netology GET {url} failed: {e!r}")
        return None


async def fetch_netology_deadlines() -> tuple[list, list, set]:
    """Возвращает (homework_list, webinars_list)."""
    print("Netology: начинаем парсинг...")

    cookie = await auth_netology()
    if not cookie:
        return [], [], set()

    async with httpx.AsyncClient(
        base_url=NETOLOGY_BASE_URL,
        timeout=20,
        follow_redirects=True,
        cookies={"_netology-on-rails_session": cookie},
    ) as s:

        # Список программ
        data = await _get(s, NETOLOGY_COURSES_URL)
        if not data:
            return [], [], set()

        programs = data.get("programs", [])
        if not programs:
            print("Netology: программы не найдены")
            return [], [], set()

        # Берём основной бакалавриат ТюмГУ (первый в списке или по ключевому слову)
        main_program = None
        for p in programs:
            title = p.get("title", "").lower()
            if "бакалавриат" in title or "разработка it" in title or "тюмгу" in title:
                main_program = p
                break
        if not main_program:
            main_program = programs[0]

        calendar_id = main_program.get("id")
        print(f"Netology: программа '{main_program.get('title', '')[:50]}' (id={calendar_id})")

        # Список модулей
        prof_data = await _get(s, NETOLOGY_PROGRAMS_URL.format(calendar_id=calendar_id))
        if not prof_data:
            return [], [], set()

        modules = prof_data.get("profession_modules", [])
        print(f"Netology: модулей {len(modules)}")

        now = datetime.datetime.now(tz=datetime.UTC)
        all_homework = []
        all_webinars = []
        completed_ids = set()

        # Раньше это было до 17 ПОСЛЕДОВАТЕЛЬНЫХ запросов (по модулю) —
        # суммарная задержка легко превышала 20с таймаут, которым эту функцию
        # оборачивают вызывающие (scheduler.py), особенно когда её же дёргали
        # 2-3 раза в рамках одного брифинга. Модули независимы — грузим
        # параллельно, а не по одному.
        program_ids = [pid for pid in (mod.get("program", {}).get("id") for mod in modules) if pid]
        events_list = await asyncio.gather(*(
            _get(s, NETOLOGY_EVENTS_URL.format(program_id=pid)) for pid in program_ids
        ))

        for program_id, events_data in zip(program_ids, events_list):
            if not events_data:
                continue

            program_title = events_data.get("title", f"program_{program_id}")
            if not program_title.startswith(CURRENT_SEMESTER_PREFIX):
                continue

            for lesson in events_data.get("lessons", []):
                for item in lesson.get("lesson_items", []):
                    item_type = item.get("type", "")
                    title = item.get("title", "")
                    item_id = item.get("id")
                    passed = item.get("passed", False)
                    path = item.get("path", "")
                    url = f"https://netology.ru{path}" if path else ""

                    if item_type == "webinar":
                        starts_at = item.get("starts_at")
                        if not starts_at:
                            continue
                        try:
                            start_dt = datetime.datetime.fromisoformat(
                                starts_at.replace("Z", "+00:00")
                            )
                            all_webinars.append({
                                "id": f"netology_webinar_{item_id}",
                                "title": title,
                                "course_name": program_title,
                                "starts_at": start_dt.astimezone(UFA_TZ).isoformat(),
                                "start_time": start_dt.astimezone(UFA_TZ).strftime("%H:%M"),
                                "webinar_url": item.get("webinar_url", url),
                                "source": "netology",
                            })
                        except Exception:
                            pass

                    elif item_type in ["task", "test", "quiz"] or (item_type == "text" and title.lower().startswith("домашнее задание")):
                        if passed:
                            # Раньше сданные задания просто переставали попадать
                            # в all_homework и на этом всё — старая запись в
                            # tasks.json так и оставалась done=False навсегда,
                            # ничто её не закрывало (в отличие от LMS, где для
                            # этого есть mark_lms_tasks_done). Отдаём id наверх,
                            # чтобы sync_all_tasks мог закрыть её сам.
                            completed_ids.add(f"netology_{item_id}")
                            continue
                        deadline_dt = _parse_deadline_from_title(title)

                        # Пропускаем если дедлайн прошёл более 10 дней назад
                        if deadline_dt and deadline_dt.astimezone(datetime.UTC) < now:
                            days_overdue = (now - deadline_dt.astimezone(datetime.UTC)).days
                            if days_overdue > 10:
                                continue

                        all_homework.append({
                            "id": f"netology_{item_id}",
                            "title": title,
                            "course_name": program_title,
                            "deadline": deadline_dt.isoformat() if deadline_dt else None,
                            "url": url,
                            "source": "netology",
                            "done": False,
                        })

        # С датой — впереди, без даты — следом (как в LMS)
        homework_with_deadline = sorted(
            [t for t in all_homework if t.get("deadline")],
            key=lambda t: t["deadline"]
        )
        homework_no_deadline = [t for t in all_homework if not t.get("deadline")]

        print(f"Netology: ДЗ с дедлайном={len(homework_with_deadline)}, без={len(homework_no_deadline)}, вебинаров={len(all_webinars)}")
        return homework_with_deadline + homework_no_deadline, all_webinars, completed_ids


async def fetch_netology_schedule_week(week_start: datetime.date) -> dict:
    """Расписание вебинаров Нетологии на неделю — по дням."""
    _, webinars, _ = await fetch_netology_deadlines()

    schedule_by_day: dict[str, list] = {}
    for i in range(7):
        day = week_start + datetime.timedelta(days=i)
        schedule_by_day[day.isoformat()] = []

    for webinar in webinars:
        try:
            dt = datetime.datetime.fromisoformat(webinar["starts_at"])
            day_key = dt.date().isoformat()
            if day_key in schedule_by_day:
                schedule_by_day[day_key].append({
                    "id": webinar["id"],
                    "name": webinar["title"],
                    "course_name": webinar["course_name"],
                    "description": "Вебинар",
                    "start": webinar["starts_at"],
                    "end": webinar["starts_at"],
                    "start_time": webinar["start_time"],
                    "end_time": webinar["start_time"],
                    "location": webinar.get("webinar_url", ""),
                    "source": "netology",
                })
        except Exception:
            continue

    return schedule_by_day


def _parse_netology_message(m: dict) -> dict | None:
    from parsers.mail import html_to_text

    msg_id = m.get("id")
    if not msg_id:
        return None
    created_at = m.get("created_at", "")
    try:
        dt = datetime.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        date_formatted = dt.astimezone(UFA_TZ).strftime("%d.%m.%Y %H:%M")
    except Exception:
        date_formatted = created_at

    return {
        "id": f"netology_notif_{msg_id}",
        "title": m.get("title", ""),
        "text": html_to_text(m.get("text", "")),
        "program_title": (m.get("program") or {}).get("title", ""),
        "program_id": (m.get("program") or {}).get("id"),
        "creator": m.get("creator_full_name", ""),
        "date": date_formatted,
        "created_at": created_at,
        "source": "netology_notification",
    }


async def fetch_netology_notifications() -> list:
    """Уведомления Нетологии (координаторы курсов, анонсы комьюнити и
    т.п. — отдельная лента в личном кабинете, не то же самое, что
    дедлайны/вебинары). Эндпоинт найден в JS-бандле фронтенда
    (/dist-lms/.../app.*.min.js), в открытой API-документации его нет.

    Берём ОБЩУЮ ленту (messages), а не unread_messages: у некоторых курсов
    единственное сообщение координатора могло оказаться "прочитано" в
    Нетологии ещё до того, как бот вообще начал следить (например, просто
    от захода на сайт) — тогда unread_messages его никогда не покажет.
    Дедуп на своей стороне (storage.is_seen), не полагаемся на read-статус
    Нетологии. Смотрим только первую страницу (самые свежие) — этого
    достаточно для регулярного опроса, полную историю разом даёт
    fetch_netology_all_notifications_by_course()."""
    print("Netology notifications: проверяем...")

    cookie = await auth_netology()
    if not cookie:
        return []

    async with httpx.AsyncClient(
        base_url=NETOLOGY_BASE_URL, timeout=20, follow_redirects=True,
        cookies={"_netology-on-rails_session": cookie},
    ) as s:
        data = await _get(s, NETOLOGY_ALL_NOTIFICATIONS_URL)
        if not data:
            return []

        result = [r for m in data.get("messages", []) if (r := _parse_netology_message(m))]
        print(f"Netology notifications: на первой странице={len(result)}")
        return result


async def fetch_netology_all_notifications_by_course() -> list:
    """Проходит ВСЮ историю уведомлений (все страницы) и возвращает по
    одному — самому свежему — сообщению на каждый курс/программу. Для
    разового каталога 'что писали по каждому курсу', не для регулярного
    опроса (это дорого — до total_pages запросов подряд)."""
    print("Netology notifications: собираем историю по всем курсам...")

    cookie = await auth_netology()
    if not cookie:
        return []

    async with httpx.AsyncClient(
        base_url=NETOLOGY_BASE_URL, timeout=20, follow_redirects=True,
        cookies={"_netology-on-rails_session": cookie},
    ) as s:
        latest_by_program: dict = {}
        page = 1
        total_pages = 1
        while page <= total_pages:
            data = await _get(s, NETOLOGY_ALL_NOTIFICATIONS_URL, params={"page": page})
            if not data:
                break
            total_pages = data.get("total_pages", 1)
            for m in data.get("messages", []):
                parsed = _parse_netology_message(m)
                if not parsed:
                    continue
                pid = parsed["program_id"]
                # страницы отдаются от новых к старым — первая встреча
                # program_id на этом проходе уже и есть самая свежая
                if pid not in latest_by_program:
                    latest_by_program[pid] = parsed
            page += 1

        result = list(latest_by_program.values())
        print(f"Netology notifications: курсов с уведомлениями={len(result)}")
        return result
