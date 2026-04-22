"""
Анализ учёбы: баллы, пары, посещаемость + прогноз.
"""
import httpx
from parsers.modeus import get_cached_jwt, get_person_id_from_jwt
from parsers.modeus_grades import _get_student_info, _fetch_tables, _build_indexes

THRESHOLDS = {
    "Математический анализ": {"pass": 51, "4": 76, "5": 91, "type": "exam"},
    "Физическая культура":   {"pass": 61, "type": "pass_fail"},
}
DEFAULT_THRESHOLDS = {"pass": 61, "4": 76, "5": 91, "type": "exam"}

# LXP предметы — баллы выставляются в конце семестра
LXP_SUBJECTS = {"Правоведение", "Анализ данных"}
# Предметы без посещаемости
NO_ATTENDANCE = {"Правоведение", "Анализ данных", "Физическая культура"}


def _get_thresh(ac_name: str) -> dict:
    for key, val in THRESHOLDS.items():
        if key in ac_name:
            return val
    return DEFAULT_THRESHOLDS


async def fetch_study_analysis() -> str:
    jwt_token = await get_cached_jwt()
    if not jwt_token:
        return "❌ Не удалось получить токен Modeus"
    person_id = get_person_id_from_jwt(jwt_token)
    info = await _get_student_info(jwt_token, person_id)
    if not info:
        return "❌ Не удалось получить данные студента"
    result = await _fetch_tables(jwt_token, info)
    if not result:
        return "❌ Не удалось получить таблицы оценок"

    primary, secondary = result
    cur_to_ac, _, cur_totals, _, _ = _build_indexes(primary, secondary)
    att_rates = {r["courseUnitRealizationId"]: r
                 for r in secondary.get("courseUnitRealizationAttendanceRates", [])}

    headers = {"Authorization": f"Bearer {jwt_token}"}
    unit_lessons = {}
    async with httpx.AsyncClient(timeout=30, http2=True) as client:
        seen = set()
        for c in primary.get("courseUnitRealizations", []):
            uid = c.get("courseUnitId")
            if not uid or uid in seen:
                continue
            seen.add(uid)
            try:
                r = await client.get(
                    f"https://utmn.modeus.org/courses/api/course-units/{uid}/lessons",
                    params={"representation": "simplegrid"},
                    headers=headers
                )
                if r.status_code == 200:
                    d = r.json()
                    unit_lessons[uid] = {
                        "total": d.get("lessonsNumber", 0),
                        "hours": d.get("courseUnit", {}).get("lessonsHours", 0),
                    }
            except Exception:
                pass

    from collections import defaultdict
    ac_groups = defaultdict(list)
    for c in primary.get("courseUnitRealizations", []):
        cid = c["id"]
        ac_name = cur_to_ac.get(cid, c["name"])
        uid = c.get("courseUnitId")
        bal_raw = cur_totals.get(cid)
        try:
            bal = float(str(bal_raw).replace(",", ".")) if bal_raw else 0.0
        except Exception:
            bal = 0.0
        att = att_rates.get(cid, {})
        lessons = [l for l in c.get("lessons", [])
                   if l.get("lessonType") not in ("CONS", "MID_CHECK")]
        held = sum(1 for l in lessons if l.get("eventHoldingStatus") == "HELD")
        ul = unit_lessons.get(uid, {})
        ac_groups[ac_name].append({
            "name": c["name"], "bal": bal,
            "present": att.get("presentRate"),
            "absent": att.get("absentRate"),
            "held": held,
            "total": ul.get("total", 0),
        })

    lines = []
    import datetime
    from config import UFA_TZ
    now = datetime.datetime.now(tz=UFA_TZ)
    lines.append(f"📊 Анализ успеваемости на {now.strftime('%d.%m.%Y')}")
    lines.append("")

    for ac_name, items in sorted(ac_groups.items()):
        thresh = _get_thresh(ac_name)
        t_type = thresh.get("type", "exam")
        total_bal = sum(i["bal"] for i in items)
        total_held = sum(i["held"] for i in items)
        total_meetings = sum(i["total"] for i in items)
        remaining = total_meetings - total_held

        is_lxp = any(lxp in ac_name for lxp in LXP_SUBJECTS)
        is_fizra = "Физическая культура" in ac_name
        no_att = any(nk in ac_name for nk in NO_ATTENDANCE)

        # Прогноз при текущем темпе
        if total_held > 0 and not is_lxp and not is_fizra:
            bal_per_meeting = total_bal / total_held
            projected = round(total_bal + bal_per_meeting * remaining, 1)
            projected = min(projected, 100.0)
        else:
            projected = None

        lines.append(f"--- {ac_name} ---")

        if len(items) > 1:
            parts = " + ".join(f"{i['bal']:.1f}" for i in items)
            lines.append(f"Текущий балл: {parts} = {total_bal:.1f}/100")
        else:
            lines.append(f"Текущий балл: {total_bal:.1f}/100")

        if t_type == "pass_fail":
            lines.append(f"Нужно для зачёта: 61 | осталось набрать: {max(0, 61 - total_bal):.1f}")
        else:
            lines.append(f"До 3: +{max(0, thresh['pass'] - total_bal):.1f} | До 4: +{max(0, thresh['4'] - total_bal):.1f} | До 5: +{max(0, thresh['5'] - total_bal):.1f}")

        lines.append(f"Встреч: {total_held} прошло из {total_meetings} (осталось {remaining})")

        if projected is not None:
            lines.append(f"Прогноз при текущем темпе: ~{projected}/100")

        if is_lxp:
            lines.append("Формат LXP: баллы выставляются преподавателем в конце семестра")

        if is_fizra:
            lines.append("Физра: данные о посещениях не поступают в Modeus, фактически занятия идут")

        if not no_att:
            for item in items:
                p = item.get("present")
                a = item.get("absent")
                if p is not None and (p > 0 or a > 0):
                    lines.append(f"Посещаемость: П {p:.0%} / Н {a:.0%}")
                    break

        lines.append("")

    return "\n".join(lines)
