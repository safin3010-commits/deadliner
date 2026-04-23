import re
import json
import os
import datetime
import httpx
from config import LMS_USERNAME, LMS_PASSWORD, UFA_TZ

LMS_BASE_URL = "https://lms.utmn.ru"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

MONTHS_RU = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


def _parse_ru_date(text: str) -> datetime.datetime | None:
    """Парсим дату вида 'понедельник, 20 апреля 2026, 00:00'"""
    m = re.search(
        r'(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+(\d{4})[,\s]+(\d{1,2}:\d{2})?',
        text, re.IGNORECASE
    )
    if not m:
        return None
    try:
        day = int(m.group(1))
        month = MONTHS_RU.get(m.group(2).lower(), 0)
        year = int(m.group(3))
        time_str = m.group(4) or "23:59"
        hour, minute = map(int, time_str.split(":"))
        return datetime.datetime(year, month, day, hour, minute, tzinfo=UFA_TZ)
    except Exception:
        return None


def _parse_deadline_from_page(html: str) -> datetime.datetime | None:
    """Извлекаем дедлайн из страницы задания/теста."""
    # Формат assign: <strong>Срок сдачи</strong> день, DD месяц YYYY, HH:MM
    blocks = re.findall(
        r'(?:Срок сдачи|Due date)[^<]{0,20}</strong>\s*([^<]{5,80})',
        html, re.IGNORECASE
    )
    for block in blocks:
        dt = _parse_ru_date(block)
        if dt:
            return dt

    # Альтернативный формат
    blocks2 = re.findall(
        r'</strong>\s*(\w+,\s*\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+\d{4}[^<]{0,20})',
        html, re.IGNORECASE
    )
    for block in blocks2:
        dt = _parse_ru_date(block)
        if dt:
            return dt

    return None


def _is_graded(grade_text: str) -> bool:
    """Задание выполнено если есть оценка (не прочерк)."""
    g = grade_text.strip()
    return g not in ("-", "", "—") and g is not None



async def fetch_lms_deadlines() -> list:
    print("LMS: начинаем парсинг...")

    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=HEADERS) as client:
        try:
            # Логин
            r = await client.get(f"{LMS_BASE_URL}/login/index.php")
            lt = re.search(r'name="logintoken"\s+value="([^"]+)"', r.text)
            login_token = lt.group(1) if lt else ""

            await client.post(f"{LMS_BASE_URL}/login/index.php", data={
                "username": LMS_USERNAME, "password": LMS_PASSWORD,
                "logintoken": login_token, "anchor": "",
            }, headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded",
                        "Referer": f"{LMS_BASE_URL}/login/index.php", "Origin": LMS_BASE_URL})

            r_my = await client.get(f"{LMS_BASE_URL}/my/")
            if "login" in str(r_my.url).lower():
                print("LMS: логин не удался")
                return []

            sk = re.search(r'"sesskey":"([^"]+)"', r_my.text)
            sesskey = sk.group(1) if sk else ""
            print(f"LMS: авторизация успешна ✅")

            # Список курсов
            r = await client.post(
                f"{LMS_BASE_URL}/lib/ajax/service.php",
                params={"sesskey": sesskey},
                json=[{"index": 0,
                       "methodname": "core_course_get_enrolled_courses_by_timeline_classification",
                       "args": {"offset": 0, "limit": 50, "classification": "inprogress",
                                "sort": "fullname", "customfieldname": "", "customfieldvalue": ""}}],
                headers={**HEADERS, "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"},
            )
            data = r.json()
            courses = []
            if isinstance(data, list) and data and not data[0].get("error"):
                courses = data[0].get("data", {}).get("courses", [])
            print(f"LMS: курсов: {len(courses)}")

            now = datetime.datetime.now(tz=UFA_TZ)
            all_tasks = []
            seen_ids = set()
            completed_ids = set()

            for course in courses:
                course_id = course.get("id")
                course_name = course.get("fullname", "LMS")

                # Страница оценок — все задания курса
                r_grades = await client.get(
                    f"{LMS_BASE_URL}/grade/report/user/index.php?id={course_id}"
                )
                html = r_grades.text

                # Находим все строки с заданиями
                rows = re.findall(r'<tr[^>]*>.*?</tr>', html, re.DOTALL)
                for row in rows:
                    if '/mod/assign/' not in row and '/mod/quiz/' not in row:
                        continue

                    # Название и ссылка
                    link_m = re.search(r'href="([^"]*mod/(?:assign|quiz)/view\.php\?id=(\d+))"[^>]*>([^<]+)</a>', row)
                    if not link_m:
                        continue
                    link = link_m.group(1)
                    mod_id = link_m.group(2)
                    task_name = link_m.group(3).strip()

                    # Оценка — берём первое значение из первого <td>
                    # (оценка идёт первым словом в ячейке, после неё идёт HTML с кнопками)
                    grade = "-"
                    first_td = re.search(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
                    if first_td:
                        td_text = re.sub(r'<[^>]+>', ' ', first_td.group(1))
                        first_word = td_text.strip().split()[0] if td_text.strip() else "-"
                        grade = first_word
                    if _is_graded(grade):
                        completed_ids.add(f"lms_{mod_id}")
                        print(f"  ✓ выполнено: {task_name[:40]} (оценка: {grade})")
                        continue  # Уже выполнено — не добавляем

                    task_id = f"lms_{mod_id}"
                    if task_id in seen_ids:
                        continue
                    seen_ids.add(task_id)

                    # Дедлайн и статус со страницы задания
                    r_task = await client.get(link)
                    deadline_dt = _parse_deadline_from_page(r_task.text)

                    # Пропускаем если дедлайн прошёл более 10 дней назад
                    if deadline_dt and deadline_dt < now:
                        days_overdue = (now - deadline_dt).days
                        if days_overdue > 10:
                            continue

                    all_tasks.append({
                        "id": task_id,
                        "title": task_name,
                        "course_name": course_name[:60],
                        "deadline": deadline_dt.isoformat() if deadline_dt else None,
                        "url": link,
                        "source": "lms",
                        "done": False,
                    })
                    print(f"  + {course_name[:25]} — {task_name[:35]}")

            # Сортируем: сначала с дедлайном, потом без
            with_deadline = sorted(
                [t for t in all_tasks if t["deadline"]],
                key=lambda x: x["deadline"]
            )
            without_deadline = [t for t in all_tasks if not t["deadline"]]
            result = with_deadline + without_deadline

            print(f"LMS: заданий: {len(result)} (с дедлайном: {len(with_deadline)}, без даты: {len(without_deadline)}), выполнено: {len(completed_ids)}")
            return result, completed_ids

        except Exception as e:
            print(f"LMS fetch failed: {e}")
            import traceback
            traceback.print_exc()
            return [], set()


LMS_GRADES_CACHE_FILE = "data/lms_grades_cache.json"
LMS_GRADES_SENT_FILE = "data/lms_grades_sent.json"


def _load_lms_grades_cache() -> dict:
    try:
        with open(LMS_GRADES_CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_lms_grades_cache(data: dict):
    os.makedirs("data", exist_ok=True)
    tmp = LMS_GRADES_CACHE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, LMS_GRADES_CACHE_FILE)


def _load_lms_grades_sent() -> set:
    try:
        with open(LMS_GRADES_SENT_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_lms_grades_sent(sent: set):
    os.makedirs("data", exist_ok=True)
    tmp = LMS_GRADES_SENT_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(list(sent), f)
    os.replace(tmp, LMS_GRADES_SENT_FILE)


async def fetch_lms_grades_changes() -> list:
    """
    Проверяем новые/изменённые оценки в LMS.
    Возвращает список dict для уведомлений.
    """
    print("LMS grades: проверяем оценки...")
    changes = []

    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=HEADERS) as client:
        try:
            r = await client.get(f"{LMS_BASE_URL}/login/index.php")
            lt = re.search(r'name="logintoken"\s+value="([^"]+)"', r.text)
            login_token = lt.group(1) if lt else ""
            await client.post(f"{LMS_BASE_URL}/login/index.php", data={
                "username": LMS_USERNAME, "password": LMS_PASSWORD,
                "logintoken": login_token, "anchor": "",
            }, headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded",
                        "Referer": f"{LMS_BASE_URL}/login/index.php", "Origin": LMS_BASE_URL})

            r_my = await client.get(f"{LMS_BASE_URL}/my/")
            if "login" in str(r_my.url).lower():
                print("LMS grades: логин не удался")
                return []

            sk = re.search(r'"sesskey":"([^"]+)"', r_my.text)
            sesskey = sk.group(1) if sk else ""

            r_courses = await client.post(
                f"{LMS_BASE_URL}/lib/ajax/service.php",
                params={"sesskey": sesskey},
                json=[{"index": 0,
                       "methodname": "core_course_get_enrolled_courses_by_timeline_classification",
                       "args": {"offset": 0, "limit": 50, "classification": "inprogress",
                                "sort": "fullname", "customfieldname": "", "customfieldvalue": ""}}],
                headers={**HEADERS, "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"},
            )
            data = r_courses.json()
            courses = []
            if isinstance(data, list) and data and not data[0].get("error"):
                courses = data[0].get("data", {}).get("courses", [])

            cache = _load_lms_grades_cache()
            sent = _load_lms_grades_sent()

            for course in courses:
                course_id = str(course.get("id"))
                course_name = course.get("fullname", "LMS")

                r_grades = await client.get(
                    f"{LMS_BASE_URL}/grade/report/user/index.php?id={course_id}"
                )
                html = r_grades.text
                rows = re.findall(r'<tr[^>]*>.*?</tr>', html, re.DOTALL)

                for row in rows:
                    if '/mod/assign/' not in row and '/mod/quiz/' not in row:
                        continue

                    link_m = re.search(r'href="([^"]*mod/(?:assign|quiz)/view\.php\?id=(\d+))"[^>]*>([^<]+)</a>', row)
                    if not link_m:
                        continue

                    mod_id = link_m.group(2)
                    task_name = link_m.group(3).strip()

                    # Берём оценку из первого td
                    first_td = re.search(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
                    grade = "-"
                    if first_td:
                        td_text = re.sub(r'<[^>]+>', ' ', first_td.group(1))
                        first_word = td_text.strip().split()[0] if td_text.strip() else "-"
                        grade = first_word

                    cache_key = f"{course_id}_{mod_id}"
                    old_grade = cache.get(cache_key, {}).get("grade")
                    sent_key = f"{cache_key}_{grade}"

                    # Новая оценка или изменилась, и ещё не отправляли
                    if grade != "-" and grade != old_grade and _is_graded(grade) and sent_key not in sent:
                        changes.append({
                            "type": "lms_grade",
                            "course_name": course_name,
                            "title": task_name,
                            "value": grade,
                            "old_value": old_grade if old_grade and old_grade != "-" else None,
                            "_sent_key": sent_key,
                        })
                        print(f"  LMS новая оценка: {course_name[:25]} — {task_name[:30]} = {grade}")

                    # Обновляем кэш
                    cache[cache_key] = {"grade": grade, "title": task_name, "course": course_name}

            _save_lms_grades_cache(cache)

            print(f"LMS grades: проверено, изменений: {len(changes)}")
            return changes

        except Exception as e:
            print(f"LMS grades check error: {e}")
            import traceback
            traceback.print_exc()
            return []
