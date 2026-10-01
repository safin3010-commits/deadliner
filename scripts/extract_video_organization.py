#!/usr/bin/env python3
"""Извлечение организационных фрагментов из WebVTT-субтитров.

Скрипт не сохраняет весь транскрипт в память наставника. Он разбирает VTT на
фразы, находит фрагменты вокруг организационных ключевых слов и добавляет их
в data/study_knowledge.json со ссылкой на таймкод. При наличии Claude CLI
можно передать найденные фрагменты на структурирование через --claude.

Пример:
  python3 scripts/extract_video_organization.py \
    --input /path/to/webinar.vtt \
    --course 'Базы данных' \
    --webinar 'Вебинар 2. Базовые элементы SQL' \
    --date 2026-09-11 \
    --claude
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = PROJECT_DIR / "data" / "study_knowledge.json"

ORGANIZATIONAL_RE = re.compile(
    r"дедлайн|сдать|сдач|домашк|домашн|лаборатор|тест|зач[её]т|экзамен|"
    r"важн|организац|срок|вопрос|вебинар|занят|рекоменд|запис[ьи]|"
    r"личном кабинете|личный кабинет|ссылк|установ|скач|нужно будет|"
    r"следующ(ем|ее|ая)|на следующ|ограничить выборку|документац",
    re.IGNORECASE,
)
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from study_profile import DISCRETE_GROUP, english_teacher_ok, matches_group, person_re  # noqa: E402

PERSON_RE = person_re()

CUE_RE = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2}\.\d{3})\s+-->\s+"
    r"(?P<end>\d{2}:\d{2}:\d{2}\.\d{3})\s*\n(?P<text>.*?)(?=\n\s*\n|\Z)",
    re.DOTALL,
)


def _time_to_seconds(value: str) -> float:
    hours, minutes, seconds = value.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _clean_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_vtt(path: Path) -> list[dict]:
    raw = path.read_text(encoding="utf-8")
    cues = []
    for match in CUE_RE.finditer(raw):
        text = _clean_text(match.group("text"))
        if text:
            cues.append({
                "start": match.group("start"),
                "start_seconds": _time_to_seconds(match.group("start")),
                "text": text,
            })
    return cues


def candidate_blocks(cues: list[dict], radius: int = 2) -> list[dict]:
    """Возвращает уникальные окна вокруг организационных фраз."""
    indexes = set()
    for index, cue in enumerate(cues):
        if ORGANIZATIONAL_RE.search(cue["text"]):
            indexes.update(range(max(0, index - radius), min(len(cues), index + radius + 1)))

    blocks = []
    for index in sorted(indexes):
        if blocks and index <= blocks[-1]["end_index"] + 1:
            blocks[-1]["end_index"] = index
            blocks[-1]["text"] += " " + cues[index]["text"]
            continue
        blocks.append({
            "start_index": index,
            "end_index": index,
            "timestamp": cues[index]["start"],
            "text": cues[index]["text"],
        })
    return blocks


def _load_knowledge(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "updated_at": None, "facts": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _fingerprint(course: str, webinar: str, timestamp: str, text: str) -> str:
    value = "|".join((course, webinar, timestamp, text[:800]))
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def _source_allowed(args: argparse.Namespace) -> bool:
    course = args.course.lower()
    if "иностранный язык" in course or "английский" in course:
        return english_teacher_ok(args.teacher or "")
    if "дискретная математика" in course and args.lesson_type == "practice":
        return matches_group(DISCRETE_GROUP, args.group or "")
    return True


def _append_personal_mentions(args: argparse.Namespace, cues: list[dict], output: Path) -> int:
    mentions = [cue for cue in cues if PERSON_RE.search(cue["text"])]
    if not mentions:
        return 0
    knowledge = _load_knowledge(output)
    existing = {fact.get("id") for fact in knowledge.get("facts", [])}
    added = 0
    for cue in mentions:
        fact_id = f"video-personal-{_fingerprint(args.course, args.webinar, cue['start'], cue['text'])}"
        if fact_id in existing:
            continue
        knowledge.setdefault("facts", []).append({
            "id": fact_id,
            "course": args.course,
            "webinar": args.webinar,
            "kind": "personal_mention",
            "importance": "high",
            "text": f"В видео прозвучало упоминание: {cue['text']}",
            "source_timestamp": cue["start"],
            "source_file": str(Path(args.input).name),
            "source_date": args.date,
            "source_url": args.source_url or None,
            "status": "active",
            "surface": "personal",
        })
        added += 1
    knowledge["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(knowledge, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return added


def _append_candidates(args: argparse.Namespace, blocks: list[dict], output: Path) -> int:
    knowledge = _load_knowledge(output)
    existing = {fact.get("id") for fact in knowledge.get("facts", [])}
    added = 0
    for block in blocks:
        fact_id = f"candidate-{_fingerprint(args.course, args.webinar, block['timestamp'], block['text'])}"
        if fact_id in existing:
            continue
        knowledge.setdefault("facts", []).append({
            "id": fact_id,
            "course": args.course,
            "webinar": args.webinar,
            "kind": "candidate",
            "importance": "needs_review",
            "text": block["text"],
            "source_timestamp": block["timestamp"],
            "source_file": str(Path(args.input).name),
            "source_date": args.date,
            "source_url": args.source_url or None,
            "status": "needs_review",
            "surface": "never_automatic",
        })
        added += 1

    knowledge["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(knowledge, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return added


def _claude_extract(args: argparse.Namespace, blocks: list[dict]) -> list[dict]:
    claude = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    if not claude or not Path(claude).is_file():
        raise RuntimeError("Claude CLI не найден; запусти без --claude для сохранения кандидатов")
    source = "\n\n".join(
        f"[{block['timestamp']}] {block['text']}" for block in blocks
    )
    prompt = f"""Ты извлекаешь только организационные сведения из субтитров учебного вебинара.
Курс: {args.course}
Вебинар: {args.webinar}
Дата: {args.date or 'не указана'}

Из текста ниже выбери только факты, которые влияют на учёбу: дедлайны, обязательность,
правила сдачи, формат лабораторных/тестов, ссылки, порядок тем, нужные программы,
предупреждения преподавателя и важные рабочие правила. Не включай обычное объяснение SQL.
Отвечай ТОЛЬКО валидным JSON-массивом объектов с полями:
kind, importance (high|normal), text, source_timestamp, status (active|needs_review), surface.
Не придумывай даты. Если фраза неясна, status=needs_review.

Субтитры:
{source}
"""
    result = subprocess.run(
        [claude, "-p", prompt, "--allowedTools", "", "--permission-prompts", "none",
         "--model", "claude-haiku-4-5-20251001"],
        cwd=PROJECT_DIR,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise RuntimeError(details[:500] or "Claude CLI вернул ошибку")
    text = result.stdout.strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < start:
        raise RuntimeError("Ответ Claude не содержит JSON-массив")
    data = json.loads(text[start:end + 1])
    return data if isinstance(data, list) else []


def _append_claude_facts(args: argparse.Namespace, facts: list[dict], output: Path) -> int:
    knowledge = _load_knowledge(output)
    existing = {fact.get("id") for fact in knowledge.get("facts", [])}
    added = 0
    for fact in facts:
        text = str(fact.get("text", "")).strip()
        if not text:
            continue
        timestamp = str(fact.get("source_timestamp", ""))
        fact_id = f"video-{_fingerprint(args.course, args.webinar, timestamp, text)}"
        if fact_id in existing:
            continue
        knowledge.setdefault("facts", []).append({
            "id": fact_id,
            "course": args.course,
            "webinar": args.webinar,
            "kind": fact.get("kind", "organization"),
            "importance": fact.get("importance", "normal"),
            "text": text,
            "source_timestamp": timestamp,
            "source_file": str(Path(args.input).name),
            "source_date": args.date,
            "source_url": args.source_url or None,
            "status": fact.get("status", "needs_review"),
            "surface": fact.get("surface", "when_relevant"),
        })
        added += 1
    knowledge["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(knowledge, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return added


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="путь к .vtt")
    parser.add_argument("--course", required=True)
    parser.add_argument("--webinar", required=True)
    parser.add_argument("--date")
    parser.add_argument("--source-url")
    parser.add_argument("--teacher", help="преподаватель видео; нужен для фильтра английского")
    parser.add_argument("--group", help="группа видео; для практики дискретной математики — STUDY_DISCRETE_GROUP из .env")
    parser.add_argument("--lesson-type", choices=("lecture", "practice"), default="lecture")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--claude", action="store_true", help="структурировать кандидаты через Claude CLI")
    args = parser.parse_args()

    input_path = Path(args.input).expanduser()
    if not input_path.exists():
        parser.error(f"файл не найден: {input_path}")
    if not _source_allowed(args):
        print("Видео пропущено по фильтру преподавателя или группы")
        return 0
    cues = parse_vtt(input_path)
    output = Path(args.output).expanduser()
    personal_added = _append_personal_mentions(args, cues, output)
    blocks = candidate_blocks(cues)
    if not blocks:
        print(f"Организационные фрагменты не найдены; упоминаний имени добавлено: {personal_added}")
        return 0

    if args.claude:
        try:
            added = _append_claude_facts(args, _claude_extract(args, blocks), output)
            print(f"Добавлено структурированных фактов: {added}; упоминаний имени: {personal_added}")
            return 0
        except Exception as error:
            print(f"Claude недоступен ({error}); сохраняю кандидатов для ручной проверки")

    added = _append_candidates(args, blocks, output)
    print(f"Добавлено фрагментов-кандидатов: {added}; упоминаний имени: {personal_added}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
