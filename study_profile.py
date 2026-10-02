"""
Личные учебные фильтры (ФИО, преподаватель, группы) — из .env, а не из кода:
репозиторий публичный. Используют скрипты разбора материалов Нетологии
(scripts/sync_netology_videos.py, extract_video_organization.py,
build_syllabus_organization.py). Пустое значение = фильтр выключен.

.env:
  STUDY_FULL_NAME=Имя Фамилия          # упоминания тебя в субтитрах/силлабусах
  STUDY_ENGLISH_TEACHER=Фамилия        # чьи записи английского брать
  STUDY_DISCRETE_GROUP=ЛБ99            # группа практик дискретной математики (пример)
  STUDY_READING_GROUP=П-99             # группа аналитического чтения (пример)
  STUDY_GROUPS=ЛБ-99,П-99,П-98         # все свои группы через запятую (календарь группы)
"""
import os
import re

try:
    import config  # noqa: F401  — подгружает .env (load_dotenv)
except Exception:
    pass

FULL_NAME = os.getenv("STUDY_FULL_NAME", "").strip()
ENGLISH_TEACHER = os.getenv("STUDY_ENGLISH_TEACHER", "").strip()
DISCRETE_GROUP = os.getenv("STUDY_DISCRETE_GROUP", "").strip()
READING_GROUP = os.getenv("STUDY_READING_GROUP", "").strip()


def study_groups() -> list[str]:
    """Все свои группы: STUDY_GROUPS + группы дискретки и чтения."""
    groups = [g.strip() for g in os.getenv("STUDY_GROUPS", "").split(",") if g.strip()]
    for g in (DISCRETE_GROUP, READING_GROUP):
        if g and g not in groups:
            groups.append(g)
    return groups


def person_re() -> re.Pattern:
    """«Имя Фамилия» в любом порядке; без настройки — не совпадает ни с чем."""
    parts = FULL_NAME.split()
    if len(parts) < 2:
        return re.compile(r"(?!x)x")
    a, b = map(re.escape, parts[:2])
    return re.compile(rf"(?:{a}\s+{b}|{b}\s+{a})", re.IGNORECASE)


def group_re(group: str) -> re.Pattern | None:
    """«ЛБ99» → ЛБ-99 / ЛБ 99 / ЛБ99; «П-09» → П-09 / П9 и т.п."""
    m = re.match(r"^\s*([^\d\s-]+)[\s-]*0*(\d+)\s*$", group or "")
    if not m:
        return None
    return re.compile(rf"{re.escape(m.group(1))}[- ]?0*{m.group(2)}\b", re.IGNORECASE)


def english_teacher_ok(text: str) -> bool:
    return bool(ENGLISH_TEACHER) and ENGLISH_TEACHER.lower() in (text or "").lower()


def matches_group(group: str, text: str) -> bool:
    rx = group_re(group)
    return bool(rx and rx.search(text or ""))
