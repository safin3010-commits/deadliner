#!/usr/bin/env python3
"""Первичная проверка организационных фрагментов из субтитров.

Извлечение субтитров специально работает широко и оставляет фрагменты со
статусом ``needs_review``. Этот скрипт переводит в активную память только
фрагменты с явными формулировками сдачи, дедлайна, доступа к материалам,
расписания, записи или коммуникации и только из вебинаров/занятий. Остальное
остаётся сохранённым для более поздней ручной проверки.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent
KNOWLEDGE_PATH = PROJECT_DIR / "data" / "study_knowledge.json"

# Намеренно строгие фразы: одно слово «вопрос» или «дедлайн» в объяснении
# теории не должно автоматически становиться организационным объявлением.
HIGH_CONFIDENCE_RE = re.compile(
    r"срок\s+сдачи|сдать\s+(?:до|к)|последн\w{0,12}\s+срок|поставлю\s+срок|"
    r"загрузить.{0,45}(?:в|на)\s+(?:личн|lms|портал|кабинет)|прикрепить|"
    r"отправить.{0,35}(?:задани|работ|файл)|в\s+личном\s+кабинете|"
    r"дедлайн.{0,35}(?:сдать|задани|работ)|"
    r"домашн\w{0,20}(?:задани|работ).{0,55}(?:сдать|выполн|прикреп)|"
    r"лабораторн\w{0,30}(?:сдать|загруз|выполн|зачт)|"
    r"тест\w{0,15}.{0,30}(?:пройти|зачет|сдать|дедлайн)|"
    r"запис\w{0,12}.{0,35}(?:появ|доступ|вылож)|"
    r"вопрос\w{0,12}.{0,35}(?:чат|писать|задать|поднять)|"
    r"скачать.{0,30}(?:файл|материал|презентац)|"
    r"установить.{0,30}(?:программ|средств)|"
    r"обязательн\w{0,20}.{0,30}(?:сдать|загруз|выполн)|"
    r"балл\w{0,15}.{0,30}(?:зачет|тест|оцен)",
    re.IGNORECASE | re.DOTALL,
)


def _is_lesson_context(fact: dict) -> bool:
    title = str(fact.get("webinar") or "").lower()
    return title.startswith(("вебинар", "🎬")) or "группа" in title or "лекция" in title


def _kind(text: str) -> str:
    if re.search(r"дедлайн|срок\s+сдачи|сдать\s+(?:до|к)", text, re.I):
        return "deadline"
    if re.search(r"домашн|лаборатор|тест", text, re.I):
        return "homework"
    if re.search(r"чат|вопрос|писать|поднять", text, re.I):
        return "communication"
    if re.search(r"скачать|ссылк|личном кабинете|установить", text, re.I):
        return "materials"
    if re.search(r"запис|следующ|расписан", text, re.I):
        return "schedule"
    return "course_organization"


def _fingerprint(fact: dict) -> tuple[str, str, str]:
    return (
        str(fact.get("course", "")).strip().lower(),
        str(fact.get("webinar", "")).strip().lower(),
        re.sub(r"\s+", " ", str(fact.get("text", "")).strip().lower())[:500],
    )


def main() -> int:
    if not KNOWLEDGE_PATH.exists():
        raise SystemExit(f"Файл не найден: {KNOWLEDGE_PATH}")
    knowledge = json.loads(KNOWLEDGE_PATH.read_text(encoding="utf-8"))
    facts = knowledge.get("facts", [])
    backup = KNOWLEDGE_PATH.with_name("study_knowledge.before_review.json")
    shutil.copy2(KNOWLEDGE_PATH, backup)

    active_keys = {_fingerprint(fact) for fact in facts if fact.get("status") == "active"}
    reviewed = 0
    skipped_context = 0
    triaged = 0
    review_time = dt.datetime.now(dt.timezone.utc).isoformat()
    for fact in facts:
        if fact.get("kind") != "candidate" or fact.get("status") != "needs_review":
            continue
        fact.setdefault("reviewed_at", review_time)
        fact.setdefault("review_method", "strict_rule_review")
        fact["review_decision"] = "ambiguous"
        triaged += 1
        text = str(fact.get("text", ""))
        if not _is_lesson_context(fact):
            fact["review_decision"] = "outside_lesson_context"
            fact["status"] = "not_organization"
            skipped_context += 1
            continue
        if not HIGH_CONFIDENCE_RE.search(text):
            continue
        key = _fingerprint(fact)
        if key in active_keys:
            fact["status"] = "reviewed_duplicate"
            continue
        fact["kind"] = _kind(text)
        fact["status"] = "active"
        fact["surface"] = "when_relevant"
        fact["review_method"] = "strict_rule_review"
        fact["reviewed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        active_keys.add(key)
        reviewed += 1

    knowledge["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    KNOWLEDGE_PATH.write_text(json.dumps(knowledge, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    remaining = sum(f.get("status") == "needs_review" for f in facts)
    print(f"Переведено в активные организационные факты: {reviewed}")
    print(f"Оставлено на ручную проверку: {remaining}")
    print(f"Фрагментов прошло триаж: {triaged}")
    print(f"Пропущено вне контекста вебинара/занятия: {skipped_context}")
    print(f"Резервная копия: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
