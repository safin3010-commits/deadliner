#!/usr/bin/env python3
"""Собирает организационные сведения из локальной выгрузки материалов Нетологии.

Скрипт не пытается превращать предметное объяснение в организационный факт.
Он берёт каталог вебинаров, дедлайны и явно организационные фрагменты силлабусов.
Фильтры пользователя:
  * иностранный язык — только преподаватель STUDY_ENGLISH_TEACHER;
  * дискретная математика — лекции все, практика только STUDY_DISCRETE_GROUP;
  * упоминания самого студента (STUDY_FULL_NAME) — отдельный тип факта.
  Значения — в .env (study_profile.py), не в коде: репозиторий публичный.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent
SOURCE_DIR = PROJECT_DIR / "netology_conspekty"
OUTPUT = PROJECT_DIR / "data" / "study_knowledge.json"

COURSE_RE = re.compile(r"^#\s*3 семестр:\s*(.+?)\s*$", re.IGNORECASE)
DEADLINE_RE = re.compile(r"дедлайн|сдать|сдач", re.IGNORECASE)
WEBINAR_RE = re.compile(r"🎙|🎬\s+(?:Видео|Вебинар)|(?:^|-).*вебинар", re.IGNORECASE)
ORG_RE = re.compile(
    r"форма контроля|итоговая оценка|максимальн.{0,20}балл|балльно|"
    r"шкала оцен|текущий контроль|промежуточная аттестация|зач[её]т|"
    r"экзамен|задание с проверкой|домашн|лаборатор|тесты?\s+по\s+тем|"
    r"итоговый тест|чек.пойнт|защит(?:а|е|у)\s+(?:проект|проекта)|"
    r"запис[ьи].{0,20}доступ|личном кабинете|видеолекц|вебинар|"
    r"сдать|сдач|оценив|обратн(?:ой|ую) связ|команд[аы].{0,20}(?:из|формир|сда|презент)",
    re.IGNORECASE,
)
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from study_profile import person_re  # noqa: E402

PERSON_RE = person_re()


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def fact_id(prefix: str, course: str, text: str) -> str:
    digest = hashlib.sha1(f"{prefix}|{course}|{text}".encode("utf-8")).hexdigest()[:16]
    return f"syllabus-{prefix}-{digest}"


def catalog_key(kind: str, course: str, text: str) -> str:
    """Схлопывает дубль из верхнего каталога и повторного блока задания."""
    value = text.lower()
    value = re.sub(r"^[-📝✅\s]+", "", value)
    value = re.sub(r"^3 семестр:\s*[^—]+—\s*", "", value)
    value = re.sub(r"^##\s*занятие\s+\d+\s*:\s*", "", value)
    value = re.sub(r"^(?:задание|тест|материал)\s*:\s*", "", value)
    return f"{kind}|{course.lower()}|{clean(value)}"


def selected_line(course: str, line: str) -> bool:
    """Применяет пользовательские фильтры к строкам каталога."""
    low_course = course.lower()
    if "иностранный язык" in low_course:
        return "мастерских" in line.lower()
    if "дискретная математика" in low_course:
        # В текущей выгрузке лекции явно помечены словом «Лекция».
        # Практика попадёт только если рядом указана нужная группа.
        if "лекц" in line.lower():
            return True
        return "лб18" in line.lower()
    return True


def add_fact(facts: list[dict], *, course: str, kind: str, text: str,
             source_file: str, source_line: int, importance: str = "normal",
             surface: str = "when_relevant", webinar: str | None = None) -> None:
    text = clean(text)
    if not text:
        return
    fact = {
        "id": fact_id(kind, course, text),
        "course": course,
        "webinar": webinar,
        "kind": kind,
        "importance": importance,
        "text": text,
        "source_file": source_file,
        "source_line": source_line,
        "status": "active",
        "surface": surface,
    }
    if any(existing.get("id") == fact["id"] for existing in facts):
        return
    if kind in {"deadline", "webinar_catalog"}:
        key = catalog_key(kind, course, text)
        if any(catalog_key(kind, item.get("course", ""), item.get("text", "")) == key
               for item in facts if item.get("kind") == kind):
            return
    facts.append(fact)


def extract_file(path: Path, facts: list[dict]) -> tuple[str, int]:
    lines = path.read_text(encoding="utf-8").splitlines()
    course = path.stem.replace("3 семестр ", "", 1)
    if lines:
        match = COURSE_RE.match(lines[0])
        if match:
            course = clean(match.group(1))

    section = ""
    in_first_lesson = False
    for number, raw in enumerate(lines, 1):
        line = clean(raw)
        if not line:
            continue

        if line.startswith(("📎", "📄")):
            continue

        if line.startswith("--- конец материала"):
            in_first_lesson = False
            continue

        if line.startswith("## "):
            if DEADLINE_RE.search(line):
                add_fact(facts, course=course, kind="deadline", text=line,
                         source_file=path.name, source_line=number,
                         importance="high", surface="when_relevant")
            section = line.lower()
            in_first_lesson = bool(re.match(r"## занятие 1(?:\s*:|\s*$)", section))
            continue

        # Имя пользователя ищем в исходном материале отдельно, но с теми же
        # фильтрами преподавателя/группы, что и каталог занятий.
        if PERSON_RE.search(line) and selected_line(course, line):
            add_fact(facts, course=course, kind="personal_mention", text=line,
                     source_file=path.name, source_line=number,
                     importance="high", surface="personal")

        is_english_or_discrete = (
            "иностранный язык" in course.lower()
            or "дискретная математика" in course.lower()
        )
        is_webinar_catalog = "вебинары" in section and WEBINAR_RE.search(line)
        is_filtered_video = is_english_or_discrete and WEBINAR_RE.search(line)
        if (is_webinar_catalog or is_filtered_video) and selected_line(course, line):
            kind = "webinar_catalog"
            if "иностранный язык" in course.lower():
                kind = "english_webinar_masterskikh"
            elif "дискретная математика" in course.lower():
                kind = "discrete_lecture" if "лекц" in line.lower() else "discrete_practice_lb18"
            add_fact(facts, course=course, kind=kind, text=line,
                     source_file=path.name, source_line=number,
                     importance="normal", surface="when_relevant")
            continue

        # Дедлайны берём из отдельного каталога. Заголовки заданий с датой
        # обрабатываются выше; строки 📝/✅ намеренно не дублируем.
        if DEADLINE_RE.search(line) and "дедлайны" in section:
            add_fact(facts, course=course, kind="deadline", text=line,
                     source_file=path.name, source_line=number,
                     importance="high", surface="when_relevant")
            continue

        # Силлабус читаем только в первом занятии, где находятся форма
        # контроля, оценивание, записи, команды и правила сдачи.
        if in_first_lesson and ORG_RE.search(line):
            if not selected_line(course, line):
                continue
            add_fact(facts, course=course, kind="course_organization", text=line,
                     source_file=path.name, source_line=number,
                     importance="normal", surface="when_relevant")

    return course, len(lines)


def main() -> int:
    knowledge = {"version": 1, "updated_at": None, "scope": "Организационные сведения курса", "facts": []}
    if OUTPUT.exists():
        knowledge = json.loads(OUTPUT.read_text(encoding="utf-8"))
    # Пересобираем только факты из силлабусов; вручную подтверждённые записи
    # из видео и другие источники сохраняем.
    existing = [fact for fact in knowledge.setdefault("facts", [])
                if not str(fact.get("id", "")).startswith("syllabus-")]
    knowledge["facts"] = existing
    # Не удаляем вручную добавленные факты и сведения из видео.
    before = len(existing)
    processed = []
    for path in sorted(SOURCE_DIR.glob("3 семестр *.txt")):
        course, line_count = extract_file(path, existing)
        processed.append({"course": course, "file": path.name, "lines": line_count})

    knowledge["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    knowledge["source_catalog"] = processed
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(knowledge, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    added = len(existing) - before
    personal = sum(f.get("kind") == "personal_mention" for f in existing)
    print(f"Обработано файлов: {len(processed)}")
    print(f"Добавлено организационных фактов: {added}")
    print(f"Отдельных упоминаний студента: {personal}")
    print(f"Всего фактов в файле: {len(existing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
