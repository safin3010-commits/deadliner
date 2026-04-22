import re
import datetime
import httpx
from config import NETOLOGY_EMAIL, NETOLOGY_PASSWORD, UFA_TZ

NETOLOGY_BASE_URL = "https://netology.ru"
NETOLOGY_SIGN_IN_URL = f"{NETOLOGY_BASE_URL}/backend/api/user/sign_in"
NETOLOGY_COURSES_URL = f"{NETOLOGY_BASE_URL}/backend/api/user/programs/calendar/filters"
NETOLOGY_PROGRAMS_URL = f"{NETOLOGY_BASE_URL}/backend/api/user/professions/{{calendar_id}}/schedule"
NETOLOGY_EVENTS_URL = f"{NETOLOGY_BASE_URL}/backend/api/user/programs/{{program_id}}/schedule"

# Ищем дату в названии: "дедлайн 25.03.26", "дедлайн 30.12.2025", "до 11.01.26"
_DEADLINE_RE = re.compile(
    r"(?:дедлайн|до)\s+(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})",
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
                return None
            r.raise_for_status()
            cookie = s.cookies.get("_netology-on-rails_session")
            if not cookie:
                print("Netology: cookie не найден")
                return None
            print("Netology: авторизация успешна ✅")
            return cookie
    except Exception as e:
        print(f"Netology auth failed: {e}")
        return None


async def _get(s: httpx.AsyncClient, url: str) -> dict | list | None:
    try:
        r = await s.get(url)
        if r.status_code == 401:
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"Netology GET {url} failed: {e}")
        return None


async def fetch_netology_deadlines() -> tuple[list, list]:
    """Возвращает (homework_list, webinars_list)."""
    print("Netology: начинаем парсинг...")

    cookie = await auth_netology()
    if not cookie:
        return [], []

    async with httpx.AsyncClient(
        base_url=NETOLOGY_BASE_URL,
        timeout=20,
        follow_redirects=True,
        cookies={"_netology-on-rails_session": cookie},
    ) as s:

        # Список программ
        data = await _get(s, NETOLOGY_COURSES_URL)
        if not data:
            return [], []

        programs = data.get("programs", [])
        if not programs:
            print("Netology: программы не найдены")
            return [], []

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
            return [], []

        modules = prof_data.get("profession_modules", [])
        print(f"Netology: модулей {len(modules)}")

        now = datetime.datetime.now(tz=datetime.UTC)
        all_homework = []
        all_webinars = []

        for mod in modules:
            program_id = mod.get("program", {}).get("id")
            if not program_id:
                continue

            events_data = await _get(s, NETOLOGY_EVENTS_URL.format(program_id=program_id))
            if not events_data:
                continue

            program_title = events_data.get("title", f"program_{program_id}")

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

                    elif item_type in ["task", "test", "quiz"] and not passed:
                        deadline_dt = _parse_deadline_from_title(title)

                        # Пропускаем если дедлайн уже прошёл
                        if deadline_dt and deadline_dt.astimezone(datetime.UTC) < now:
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

        # Убираем задания без дедлайна из списка (оставляем только с датой)
        homework_with_deadline = [t for t in all_homework if t.get("deadline")]
        homework_no_deadline = [t for t in all_homework if not t.get("deadline")]

        print(f"Netology: ДЗ с дедлайном={len(homework_with_deadline)}, без={len(homework_no_deadline)}, вебинаров={len(all_webinars)}")
        return homework_with_deadline, all_webinars


async def fetch_netology_schedule_week(week_start: datetime.date) -> dict:
    """Расписание вебинаров Нетологии на неделю — по дням."""
    _, webinars = await fetch_netology_deadlines()

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
