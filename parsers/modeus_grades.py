"""
Парсер оценок из Modeus (utmn.modeus.org).

Отслеживает:
- Оценки за встречи (lessonControlObjects) — "На встрече такого числа поставлена оценка X"
- Посещаемость (eventPersonAttendances) — П/Н
- Текущий итог по курсу (courseUnitRealizationControlObjects)

Хранит seen в data/seen_modeus_grades.json
"""

import json
import os
import datetime
import httpx
from config import UFA_TZ

SEEN_FILE = "data/seen_modeus_grades.json"
BASE_URL = "https://utmn.modeus.org"
STUDENTS_API = f"{BASE_URL}/students-app/api/pages/student-card/my"

ATTENDANCE_LABELS = {
    "PRESENT": "П",
    "ABSENT": "Н",
    "LATE": "О",  # опоздание
}

MONTH_NAMES_RU = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря"
]


# ─── Хранилище ────────────────────────────────────────────────────────

def _load_seen() -> dict:
    try:
        with open(SEEN_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_seen(seen: dict) -> None:
    os.makedirs("data", exist_ok=True)
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)


# ─── Получение student info ───────────────────────────────────────────

async def _get_student_info(jwt_token: str, person_id: str) -> dict | None:
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=30, http2=True) as client:
            r = await client.get(f"{STUDENTS_API}/primary", headers=headers)
            if r.status_code != 200:
                print(f"Modeus grades: /primary → {r.status_code}")
                return None

            data = r.json()
            student_id = data.get("id")
            cf = data.get("curriculumFlow", {})

            # Берём APR который содержит сегодняшнюю дату
            aprs = data.get("academicPeriodRealizations", [])
            today = datetime.datetime.now(tz=UFA_TZ).date()
            apr_id = None

            for apr in aprs:
                name = apr.get("name", "")
                # Парсим даты из названия вида "... (09.02.2026-31.08.2026)"
                import re
                m = re.search(r'\((\d{2})\.(\d{2})\.(\d{4})-(\d{2})\.(\d{2})\.(\d{4})\)', name)
                if m:
                    try:
                        start = datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
                        end = datetime.date(int(m.group(6)), int(m.group(5)), int(m.group(4)))
                        if start <= today <= end:
                            apr_id = apr.get("id")
                            print(f"Modeus grades: текущий семестр — {name[:60]}")
                            break
                    except Exception:
                        continue

            # Фоллбэк — берём последний в списке
            if not apr_id and aprs:
                apr_id = aprs[-1].get("id")

            if not all([student_id, apr_id]):
                print("Modeus grades: не удалось получить student_id или apr_id")
                return None

            return {
                "student_id": student_id,
                "apr_id": apr_id,
                "curriculum_flow_id": cf.get("id"),
                "curriculum_plan_id": cf.get("curriculumPlanId"),
                "person_id": person_id,
            }
    except Exception as e:
        print(f"Modeus grades: ошибка student info: {e}")
        return None


# ─── Запрос таблиц оценок ─────────────────────────────────────────────

async def _fetch_tables(jwt_token: str, info: dict) -> tuple | None:
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Content-Type": "application/json",
    }
    primary_body = {
        "personId": info["person_id"],
        "withMidcheckModulesIncluded": False,
        "aprId": info["apr_id"],
        "studentId": info["student_id"],
        "curriculumFlowId": info["curriculum_flow_id"],
        "curriculumPlanId": info["curriculum_plan_id"],
    }

    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=30, http2=True) as client:
            r1 = await client.post(
                f"{STUDENTS_API}/academic-period-results-table/primary",
                json=primary_body, headers=headers
            )
            if r1.status_code != 200:
                print(f"Modeus grades: primary → {r1.status_code}")
                return None

            primary = r1.json()
            cur_ids = [c["id"] for c in primary.get("courseUnitRealizations", [])]
            if not cur_ids:
                return None

            r2 = await client.post(
                f"{STUDENTS_API}/academic-period-results-table/secondary",
                json={"courseUnitRealizationId": cur_ids},
                headers=headers
            )
            if r2.status_code != 200:
                print(f"Modeus grades: secondary → {r2.status_code}")
                return None

            return primary, r2.json()

    except Exception as e:
        print(f"Modeus grades: ошибка запроса таблиц: {e}")
        return None


# ─── Форматирование даты ──────────────────────────────────────────────

def _fmt_date(dt_str: str) -> str:
    """'2026-03-18T17:40:00' → '18 марта'"""
    try:
        dt = datetime.datetime.fromisoformat(dt_str)
        return f"{dt.day} {MONTH_NAMES_RU[dt.month - 1]}"
    except Exception:
        return dt_str[:10] if dt_str else ""


# ─── Парсинг новых оценок ─────────────────────────────────────────────

def _build_indexes(primary: dict, secondary: dict) -> tuple[dict, dict, dict, dict, dict]:
    """
    Строим вспомогательные индексы для быстрого поиска.
    Возвращает:
      - cur_to_academic: courseUnitRealizationId → название академического курса
      - lesson_info: lessonId → {name, date, course, subject}
      - cur_totals: courseUnitRealizationId → текущий итог (resultValue)
      - attendance_by_lesson: lessonId → attendanceStatus (PRESENT/ABSENT)
      - academic_courses: academicCourseId → name
    """
    # Связь cur → academic course name
    cur_to_academic = {}
    academic_courses = {}
    for ac in primary.get("academicCourses", []):
        academic_courses[ac["id"]] = ac["name"]
        for cur_id in ac.get("courseUnitRealizationIds", []):
            cur_to_academic[cur_id] = ac["name"]

    # lesson_info: id → {name, date, course}
    lesson_info = {}
    for c in primary.get("courseUnitRealizations", []):
        course_name = cur_to_academic.get(c["id"]) or c["name"]
        subject_name = c["name"]  # Конкретный предмет (Мастерская, Лекционный блок)
        for lesson in c.get("lessons", []):
            lesson_info[lesson["id"]] = {
                "name": lesson.get("name", ""),
                "date": lesson.get("eventStartsAtLocal", ""),
                "course": course_name,
                "subject": subject_name,
                "type": lesson.get("typeName", ""),
            }

    # Текущие итоги по курсу
    cur_totals = {}
    for obj in secondary.get("courseUnitRealizationControlObjects", []):
        result = obj.get("resultCurrent")
        if result:
            cur_totals[obj["courseUnitRealizationId"]] = result.get("resultValue", "")

    # Посещаемость
    attendance_by_lesson = {}
    for att in secondary.get("eventPersonAttendances", []):
        lesson_id = att.get("lessonId")
        result_id = att.get("resultId", "")
        if lesson_id:
            attendance_by_lesson[lesson_id] = result_id

    return cur_to_academic, lesson_info, cur_totals, attendance_by_lesson, academic_courses


def _parse_new_grades(primary: dict, secondary: dict, seen: dict) -> list[dict]:
    """Сравниваем с seen, возвращаем новые/изменённые оценки."""
    new_grades = []
    cur_to_academic, lesson_info, cur_totals, attendance_by_lesson, academic_courses = \
        _build_indexes(primary, secondary)

    # 1. Оценки за встречи
    for obj in secondary.get("lessonControlObjects", []):
        result = obj.get("result")
        if not result:
            continue

        value = str(result.get("resultValue") or "").strip()
        if not value:
            continue

        lesson_id = obj.get("lessonId", "")
        control_id = obj.get("controlObjectId", "")
        grade_id = f"lesson_{lesson_id}_{control_id}"

        old_value = seen.get(grade_id)
        if old_value == value:
            continue

        seen[grade_id] = value
        info = lesson_info.get(lesson_id, {})
        attendance = attendance_by_lesson.get(lesson_id, "")

        new_grades.append({
            "id": grade_id,
            "type": "lesson",
            "course": info.get("course", "Курс"),
            "subject": info.get("subject", ""),
            "lesson_name": info.get("name", ""),
            "lesson_date": info.get("date", ""),
            "lesson_type": obj.get("typeName", ""),
            "value": value,
            "old_value": old_value,
            "attendance": attendance,
            "by": result.get("updatedBy") or result.get("createdBy", ""),
            "lesson_id": lesson_id,  # нужен для поиска итога курса
        })

    # 2. Текущий итог по курсу
    for obj in secondary.get("courseUnitRealizationControlObjects", []):
        cur_id = obj.get("courseUnitRealizationId")
        result = obj.get("resultCurrent")
        if not result:
            continue

        value = str(result.get("resultValue") or "").strip()
        if not value:
            continue

        grade_id = f"cur_total_{cur_id}"
        old_value = seen.get(grade_id)
        if old_value == value:
            continue

        seen[grade_id] = value
        course = cur_to_academic.get(cur_id, "Курс")

        new_grades.append({
            "id": grade_id,
            "type": "current_total",
            "course": course,
            "subject": "",
            "value": value,
            "old_value": old_value,
            "by": result.get("updatedBy") or result.get("createdBy", ""),
        })

    # 3. Итог модуля
    for obj in secondary.get("academicCourseControlObjects", []):
        ac_id = obj.get("academicCourseId")
        value = str(obj.get("value") or "").strip()
        if not value:
            continue

        grade_id = f"ac_total_{ac_id}"
        old_value = seen.get(grade_id)
        if old_value == value:
            continue

        seen[grade_id] = value
        new_grades.append({
            "id": grade_id,
            "type": "module_total",
            "course": academic_courses.get(ac_id, "Курс"),
            "subject": "",
            "value": value,
            "old_value": old_value,
            "by": "",
        })

    return new_grades, cur_totals, cur_to_academic


# ─── Форматирование уведомления ───────────────────────────────────────

def format_grade_notification(grade: dict, cur_totals: dict = None) -> str:
    """
    Красивое уведомление об оценке.

    Пример для оценки за встречу:
    📝 Новая оценка — Modeus

    📚 История России
    📌 Мастерская "История России"
    📅 На встрече 18 марта (Практическое занятие)
       Советский Союз в 1929–1941 гг.
    🎯 Работа на встрече: *2*  |  П
    👤 Турова Елена Ивановна

    📊 Текущий итог по курсу: *4.00*
    """
    grade_type = grade.get("type", "")
    course = grade.get("course", "")
    value = grade.get("value", "")
    old_value = grade.get("old_value")

    # Эмодзи оценки
    try:
        v_num = float(str(value).replace(",", "."))
        if v_num == 0:
            mark_emoji = "⭕"
        elif v_num >= 20 or v_num >= 4:
            mark_emoji = "🟢"
        elif v_num >= 10 or v_num >= 3:
            mark_emoji = "🟡"
        else:
            mark_emoji = "🔴"
    except Exception:
        mark_emoji = "📝"

    lines = []

    if grade_type == "lesson":
        lesson_date = _fmt_date(grade.get("lesson_date", ""))
        lesson_name = grade.get("lesson_name", "")
        lesson_type = grade.get("lesson_type", "")
        subject = grade.get("subject", "")
        attendance = grade.get("attendance", "")
        by = grade.get("by", "")

        # Заголовок
        action = "изменена" if old_value else "новая"
        lines.append(f"{mark_emoji} *Оценка {action}* — Modeus\n")

        # Курс
        lines.append(f"📚 *{course}*")
        if subject and subject != course:
            lines.append(f"📌 {subject}")

        # Встреча
        date_str = f"На встрече {lesson_date}" if lesson_date else "На встрече"
        type_str = f" ({lesson_type})" if lesson_type else ""
        lines.append(f"📅 {date_str}{type_str}")
        if lesson_name:
            lines.append(f"   _{lesson_name}_")

        # Оценка и посещаемость
        att_label = ATTENDANCE_LABELS.get(attendance, "")
        att_str = f"  |  *{att_label}*" if att_label else ""
        score_type = grade.get("lesson_type", "Оценка")
        if old_value:
            lines.append(f"🎯 {score_type}: *{old_value}* → *{value}*{att_str}")
        else:
            lines.append(f"🎯 {score_type}: *{value}*{att_str}")

        if by:
            lines.append(f"👤 _{by}_")

        # Итог по курсу если есть
        if cur_totals:
            # Ищем cur_id для этого курса — через lesson_id
            lesson_id = grade.get("lesson_id", "")
            # cur_totals: cur_id → value
            # Нужно найти cur_id который относится к этому курсу
            # Простейший способ: берём из seen по ключу cur_total_*
            # Но у нас уже есть cur_totals напрямую
            # Найдём значение из cur_totals соответствующее курсу
            if grade.get("course_total"):
                lines.append(f"\n📊 Текущий итог по курсу: *{grade['course_total']}*")

    elif grade_type == "current_total":
        action = "обновлён" if old_value else "выставлен"
        lines.append(f"📊 *Текущий итог {action}* — Modeus\n")
        lines.append(f"📚 *{course}*")
        if old_value:
            lines.append(f"🎯 *{old_value}* → *{value}*")
        else:
            lines.append(f"🎯 *{value}*")
        if grade.get("by"):
            lines.append(f"👤 _{grade['by']}_")

    elif grade_type == "module_total":
        lines.append(f"🏆 *Итог модуля* — Modeus\n")
        lines.append(f"📚 *{course}*")
        if old_value:
            lines.append(f"🎯 *{old_value}* → *{value}*")
        else:
            lines.append(f"🎯 *{value}*")

    return "\n".join(lines)


# ─── Публичный интерфейс ──────────────────────────────────────────────

async def fetch_modeus_grades() -> list[dict]:
    """
    Проверяем новые/изменённые оценки в Modeus.
    Возвращает список словарей с полями для отправки уведомлений.
    """
    print("Modeus grades: проверяем оценки...")

    try:
        from parsers.modeus import get_cached_jwt, get_person_id_from_jwt

        jwt_token = await get_cached_jwt()
        if not jwt_token:
            print("Modeus grades: нет JWT токена")
            return []

        person_id = get_person_id_from_jwt(jwt_token)
        if not person_id:
            return []

        info = await _get_student_info(jwt_token, person_id)
        if not info:
            return []

        result = await _fetch_tables(jwt_token, info)
        if not result:
            return []

        primary, secondary = result
        seen = _load_seen()
        new_grades, cur_totals, cur_to_academic = _parse_new_grades(primary, secondary, seen)

        # Добавляем итог курса к оценкам за встречи
        # Строим маппинг lessonId → cur_id
        lesson_to_cur = {}
        for c in primary.get("courseUnitRealizations", []):
            for lesson in c.get("lessons", []):
                lesson_to_cur[lesson["id"]] = c["id"]

        for grade in new_grades:
            if grade["type"] == "lesson":
                lesson_id = grade.get("lesson_id", "")
                cur_id = lesson_to_cur.get(lesson_id)
                if cur_id and cur_id in cur_totals:
                    grade["course_total"] = cur_totals[cur_id]

        if new_grades:
            print(f"Modeus grades: новых оценок {len(new_grades)}")
        else:
            print("Modeus grades: новых оценок нет")
            _save_seen(seen)  # Сохраняем seen только если нет новых — иначе после отправки

        # Добавляем отформатированное сообщение
        for grade in new_grades:
            grade["_text"] = format_grade_notification(grade, cur_totals)

        # Возвращаем seen чтобы сохранить после успешной отправки
        for grade in new_grades:
            grade["_seen"] = seen
        return new_grades

    except Exception as e:
        print(f"Modeus grades fetch failed: {e}")
        import traceback
        traceback.print_exc()
        return []


async def fetch_grades_for_subject(cur_id: str) -> dict | None:
    """
    Получаем все оценки и посещаемость по конкретному предмету.
    Возвращает структуру для отображения.
    """
    try:
        from parsers.modeus import get_cached_jwt, get_person_id_from_jwt
        jwt_token = await get_cached_jwt()
        person_id = get_person_id_from_jwt(jwt_token)
        info = await _get_student_info(jwt_token, person_id)
        if not info:
            return None

        result = await _fetch_tables(jwt_token, info)
        if not result:
            return None

        primary, secondary = result
        _, lesson_info, cur_totals, attendance_by_lesson, _ = _build_indexes(primary, secondary)

        # Находим курс
        course_name = ""
        lessons_data = []
        for c in primary.get("courseUnitRealizations", []):
            if c["id"] != cur_id:
                continue
            course_name = c["name"]
            for lesson in c.get("lessons", []):
                lid = lesson["id"]
                att = attendance_by_lesson.get(lid, "")
                # Оценки за эту встречу
                scores = []
                for obj in secondary.get("lessonControlObjects", []):
                    if obj.get("lessonId") == lid and obj.get("result"):
                        scores.append({
                            "type": obj.get("typeName", ""),
                            "value": obj["result"].get("resultValue", ""),
                        })
                if att or scores:
                    lessons_data.append({
                        "name": lesson.get("name", ""),
                        "date": lesson.get("eventStartsAtLocal", ""),
                        "attendance": att,
                        "scores": scores,
                    })

        # Сортируем по дате
        lessons_data.sort(key=lambda x: x["date"] or "")

        # Считаем оставшиеся встречи — всего встреч в курсе минус прошедшие
        today = datetime.datetime.now(tz=UFA_TZ).date()
        total_lessons_count = 0
        passed_lessons_count = 0
        for c2 in primary.get("courseUnitRealizations", []):
            if c2["id"] != cur_id:
                continue
            for lesson in c2.get("lessons", []):
                total_lessons_count += 1
                lesson_date_str = lesson.get("eventStartsAtLocal", "")
                if lesson_date_str:
                    try:
                        lesson_date = datetime.datetime.fromisoformat(lesson_date_str).date()
                        if lesson_date <= today:
                            passed_lessons_count += 1
                    except Exception:
                        pass
        remaining = max(0, total_lessons_count - passed_lessons_count)

        return {
            "cur_id": cur_id,
            "course_name": course_name,
            "lessons": lessons_data,
            "total": cur_totals.get(cur_id, ""),
            "remaining_lessons": remaining if total_lessons_count > 0 else None,
        }
    except Exception as e:
        print(f"fetch_grades_for_subject error: {e}")
        return None


async def fetch_all_subjects() -> list[dict]:
    """Список всех предметов текущего семестра."""
    try:
        from parsers.modeus import get_cached_jwt, get_person_id_from_jwt
        jwt_token = await get_cached_jwt()
        person_id = get_person_id_from_jwt(jwt_token)
        info = await _get_student_info(jwt_token, person_id)
        if not info:
            return []

        result = await _fetch_tables(jwt_token, info)
        if not result:
            return []

        primary, secondary = result
        _, _, cur_totals, _, cur_to_academic = _build_indexes(primary, secondary)

        subjects = []
        for c in primary.get("courseUnitRealizations", []):
            total = cur_totals.get(c["id"], "")
            subjects.append({
                "id": c["id"],
                "name": c["name"],
                "academic": cur_to_academic.get(c["id"], c["name"]),
                "total": total,
            })
        return subjects
    except Exception as e:
        print(f"fetch_all_subjects error: {e}")
        return []
