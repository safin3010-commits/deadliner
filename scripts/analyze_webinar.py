#!/usr/bin/env python3
"""Разбор полной расшифровки вебинара на теорию и организационные факты.

Никакого предфильтра по ключевым словам — вся расшифровка (data/video_transcripts/*.txt,
уже без таймкодов, см. sync_netology_videos.py) одним куском уходит в Claude с
промптом, который просит разделить её на:

  1. организационное — всё, что практически влияет на учёбу (дедлайны, что и
     куда сдавать, изменения расписания/формата, важные объявления и т.п.);
  2. теорию — связный конспект по содержанию занятия, пригодный для того,
     чтобы потом объяснять эту тему в чате, не пересматривая вебинар заново.

Результат сохраняется в двух местах:
  * data/webinar_notes/<курс>__<item_id>__<название>.json — ПОЛНЫЙ конспект
    именно этого занятия (теория + организационное вместе) — источник для
    обучения по запросу (mentor_qa.py уже умеет проверять data/webinar_notes/
    в дополнение к сырым транскриптам);
  * data/study_knowledge.json (facts[]) — только организационные факты,
    сразу status="active" (Claude уже понял текст по смыслу, а не по
    словам-триггерам, поэтому обошлось без промежуточного needs_review) —
    именно отсюда их читают сводки (scheduler.get_relevant_knowledge_facts,
    scripts/mentor_checkin.py).

Использование:
  python3 scripts/analyze_webinar.py --item-id 3533273       # один вебинар
  python3 scripts/analyze_webinar.py --all-new                # все скачанные,
                                                                # но ещё не разобранные
  python3 scripts/analyze_webinar.py --all-new --limit 5       # порциями (для бэкфилла)
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
MANIFEST_PATH = DATA_DIR / "video_manifest.json"
KNOWLEDGE_PATH = DATA_DIR / "study_knowledge.json"
NOTES_DIR = DATA_DIR / "webinar_notes"

# Sonnet, не Haiku — тут нужно реальное понимание длинного текста и смысловое
# разделение, а не узкая структурная экстракция.
CLAUDE_MODEL = "claude-sonnet-5"
CLAUDE_TIMEOUT_SEC = 480  # расшифровка полуторачасового вебинара — длинный вход

SAFE_RE = re.compile(r"[^\wа-яА-ЯёЁ.-]+", re.UNICODE)

PROMPT_TEMPLATE = """Ты разбираешь полную текстовую расшифровку одного вебинара курса «{course}» — «{webinar}» (дата {date}). Расшифровка получена из субтитров, там встречаются опечатки распознавания речи и разорванные фразы — восстанавливай смысл, не переноси опечатки в вывод.

Раздели всё, что было сказано, на два непересекающихся потока.

1) ОРГАНИЗАЦИОННОЕ — всё, что практически влияет на учёбу студента: дедлайны и сроки сдачи, что и куда конкретно сдавать/загружать, изменения в расписании или формате занятий, важные объявления, как связаться с преподавателем по вопросам, требования к оформлению работ, критерии и веса в оценке, любые «не забудьте», «к следующему занятию» и подобное. НЕ включай сюда обычные учебные ремарки внутри объяснения теории («сейчас разберём пример», «это важно понять, потому что…») — только то, что реально организационное, а не про сам предмет.

Для каждого организационного факта: text — краткое обобщение сути (1 фраза, без цитаты внутри). Дополнительно в quote — дословная цитата преподавателя из расшифровки (одно или несколько предложений подряд, ТОЧНО как в тексте, без перефразирования и без исправления опечаток распознавания — просто аккуратно вырезанный кусок), которая подтверждает и раскрывает этот факт подробнее, чем краткое summary. Если ни одной цитаты, прямо подтверждающей факт, в расшифровке нет (например, факт восстановлен из контекста нескольких реплик) — оставь quote пустой строкой, не выдумывай цитату.

2) ТЕОРИЯ — связный конспект по существу занятия: какие темы и понятия разбирали и в каком порядке, с определениями, примерами, важными оговорками и типичными ошибками, которые называл преподаватель. Пиши так, чтобы по этому конспекту позже можно было объяснить тему человеку, который вебинар не смотрел — не пересказывай субтитры дословно и не сжимай в общие фразы, сохрани всё существенное содержание с примерами.

Если организационного не было вообще — верни пустой список. Если занятие было чисто организационным без разбора теории — верни пустую строку в theory_notes.

Ответь СТРОГО валидным JSON без markdown-обёртки (без ```), без пояснений до или после, ровно такой схемы:
{{
  "organizational": [
    {{"kind": "deadline|homework|schedule|communication|materials|course_organization", "importance": "high|normal", "text": "...", "quote": "..."}}
  ],
  "theory_notes": "конспект теории в markdown с заголовками по подтемам, или пустая строка"
}}

Расшифровка вебинара:
---
{transcript}
---"""


def _find_claude_bin() -> str:
    import os
    return shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")


def _slug(value: str, max_len: int = 80) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    value = SAFE_RE.sub("_", value).strip("._")
    return (value or "video")[:max_len]


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _strip_json_wrapper(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _call_claude(prompt: str) -> tuple[dict | None, str]:
    claude_bin = _find_claude_bin()
    import os
    if not os.path.isfile(claude_bin):
        return None, "claude CLI не найден"
    try:
        result = subprocess.run(
            [claude_bin, "-p", prompt,
             "--allowedTools", "",
             "--permission-prompts", "none",
             "--model", CLAUDE_MODEL],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return None, f"не ответил за {CLAUDE_TIMEOUT_SEC}с"
    except Exception as e:
        return None, f"ошибка запуска claude: {e!r}"

    text = _strip_json_wrapper((result.stdout or "").strip())
    if not text or result.returncode != 0:
        return None, f"пустой ответ или returncode={result.returncode}: {(result.stderr or '')[:300]}"
    try:
        # strict=False — модель иногда кладёт в multiline theory_notes
        # буквальные переводы строк вместо \n (формально невалидный JSON,
        # но однозначно читаемый); не гоняем повторный запрос из-за этого.
        data = json.loads(text, strict=False)
    except json.JSONDecodeError as e:
        # Другой частый случай — модель кладёт внутрь text() значения
        # неэкранированную прямую кавычку (например, дословную цитату
        # преподавателя) и рвёт структуру. Простой повтор всего анализа
        # дорогой и не гарантирует успех — чиним формат отдельным дешёвым
        # запросом (Haiku, без нового чтения транскрипта), не пересчитывая
        # содержание заново.
        repaired, repair_err = _repair_json_text(text)
        if repaired is not None:
            return repaired, ""
        return None, (
            f"не удалось разобрать JSON ответа: {e!r} — начало ответа: {text[:200]!r}"
            + (f" (починка тоже не удалась: {repair_err})" if repair_err else "")
        )
    return data, ""


def _repair_json_text(broken: str) -> tuple[dict | None, str]:
    claude_bin = _find_claude_bin()
    import os
    if not os.path.isfile(claude_bin):
        return None, "claude CLI не найден"
    prompt = (
        "Следующий текст должен быть валидным JSON, но не парсится (скорее всего, "
        "неэкранированная кавычка внутри значения строки). Верни РОВНО ТОТ ЖЕ "
        "контент, но синтаксически корректным JSON — ничего не меняй по смыслу, "
        "не сокращай, не переформулируй, только почини синтаксис (экранируй кавычки "
        "внутри строк как \\\", переводы строк как \\n). Ответь строго JSON, без "
        "markdown-обёртки, без пояснений.\n\n" + broken
    )
    try:
        result = subprocess.run(
            [claude_bin, "-p", prompt, "--allowedTools", "", "--permission-prompts", "none",
             "--model", "claude-haiku-4-5-20251001"],
            cwd=PROJECT_DIR, capture_output=True, text=True, timeout=120,
        )
    except Exception as e:
        return None, f"ошибка запуска: {e!r}"
    text = _strip_json_wrapper((result.stdout or "").strip())
    if not text or result.returncode != 0:
        return None, f"пустой ответ (returncode={result.returncode})"
    try:
        return json.loads(text, strict=False), ""
    except json.JSONDecodeError as e:
        return None, f"всё ещё невалиден: {e!r}"


def _fact_fingerprint(course: str, webinar: str, text: str) -> str:
    key = f"{course.strip().lower()}|{webinar.strip().lower()}|{re.sub(r'\\s+', ' ', text.strip().lower())[:400]}"
    return hashlib.md5(key.encode("utf-8")).hexdigest()


def _append_facts(item: dict, organizational: list[dict]) -> int:
    knowledge = _load_json(KNOWLEDGE_PATH, {"facts": []})
    facts = knowledge.setdefault("facts", [])
    existing = {
        _fact_fingerprint(f.get("course") or "", f.get("webinar") or "", f.get("text") or "")
        for f in facts
    }
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    added = 0
    for fact in organizational:
        text = (fact.get("text") or "").strip()
        if not text:
            continue
        fp = _fact_fingerprint(item["course"], item["item_title"], text)
        if fp in existing:
            continue
        existing.add(fp)
        facts.append({
            "kind": fact.get("kind") or "course_organization",
            "importance": fact.get("importance") or "normal",
            "course": item["course"],
            "webinar": item["item_title"],
            "date": item.get("date"),
            "text": text,
            "quote": (fact.get("quote") or "").strip(),
            "source_url": item.get("source_url"),
            "status": "active",
            "surface": "when_relevant",
            "review_method": "claude_full_transcript",
            "added_at": now,
        })
        added += 1
    if added:
        knowledge["updated_at"] = now
        _atomic_write_json(KNOWLEDGE_PATH, knowledge)
    return added


def analyze_item(item: dict, verbose: bool = True) -> bool:
    transcript_path = PROJECT_DIR / item["transcript_path"]
    if not transcript_path.exists():
        if verbose:
            print(f"  пропуск: нет транскрипта {transcript_path}")
        return False

    transcript = transcript_path.read_text(encoding="utf-8").strip()
    if not transcript:
        if verbose:
            print("  пропуск: пустой транскрипт")
        return False

    prompt = PROMPT_TEMPLATE.format(
        course=item["course"], webinar=item["item_title"],
        date=item.get("date") or "неизвестна", transcript=transcript,
    )
    data, err = _call_claude(prompt)
    if err and "разобрать JSON" in err:
        # Модель иногда не экранирует кавычку внутри text — при повторном
        # запросе тем же промптом обычно получается валидный JSON с первого
        # раза заново (не детерминированная генерация), дешевле повторить
        # один раз, чем терять весь разбор вебинара целиком.
        if verbose:
            print(f"  {err} — пробую ещё раз")
        data, err = _call_claude(prompt)
    if err:
        print(f"  ошибка разбора: {err}")
        return False

    organizational = data.get("organizational") or []
    theory_notes = (data.get("theory_notes") or "").strip()

    slug = f"{_slug(item['course'])}__{item['item_id']}__{_slug(item['item_title'])}"
    note_path = NOTES_DIR / f"{slug}.json"
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    _atomic_write_json(note_path, {
        "item_id": item["item_id"],
        "course": item["course"],
        "webinar": item["item_title"],
        "date": item.get("date"),
        "teacher": item.get("teacher"),
        "group": item.get("group"),
        "source_url": item.get("source_url"),
        "theory_notes": theory_notes,
        "organizational": organizational,
        "analyzed_at": now,
    })

    added = _append_facts(item, organizational)
    if verbose:
        print(f"  ок: организационных фактов {len(organizational)} (новых {added}), "
              f"теория {'есть' if theory_notes else 'нет'} → {note_path.relative_to(PROJECT_DIR)}")

    item["analyzed"] = True
    item["analyzed_at"] = now
    item["notes_path"] = str(note_path.relative_to(PROJECT_DIR))
    return True


def _save_manifest(manifest: dict) -> None:
    manifest["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    _atomic_write_json(MANIFEST_PATH, manifest)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--item-id", type=int, help="разобрать один конкретный вебинар по item_id")
    parser.add_argument("--all-new", action="store_true", help="разобрать все скачанные, но ещё не разобранные")
    parser.add_argument("--limit", type=int, default=None, help="ограничить число вебинаров за запуск (--all-new)")
    args = parser.parse_args()

    manifest = _load_json(MANIFEST_PATH, {"items": []})
    items = manifest.get("items", [])
    by_id = {item["item_id"]: item for item in items}

    if args.item_id:
        item = by_id.get(args.item_id)
        if not item:
            print(f"item_id {args.item_id} не найден в манифесте")
            return 1
        targets = [item]
    elif args.all_new:
        # Живая сессия = item_type="webinar" ИЛИ "video" с полем "teacher"
        # (иностранный/дискретная математика/аналитическое чтение — там
        # запись живого занятия типизирована как "video", отличить от
        # обычного предзаписанного видеокурса можно только по преподавателю
        # в названии, см. ту же оговорку в sync_netology_videos.py).
        targets = [
            i for i in items
            if i.get("status") == "downloaded"
            and (i.get("item_type") == "webinar" or i.get("teacher"))
            and not i.get("analyzed")
        ]
        if args.limit:
            targets = targets[: args.limit]
    else:
        parser.print_help()
        return 1

    if not targets:
        print("Нечего разбирать — новых скачанных и неразобранных вебинаров нет.")
        return 0

    print(f"К разбору: {len(targets)}")
    ok_count = 0
    for item in targets:
        print(f"→ {item['course']} — {item['item_title']} ({item.get('date')})")
        if analyze_item(item):
            ok_count += 1
        _save_manifest(manifest)  # сохраняем прогресс после каждого — не теряем при обрыве

    print(f"Готово: успешно разобрано {ok_count} из {len(targets)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
