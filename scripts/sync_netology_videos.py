#!/usr/bin/env python3
"""Собрать доступные видео 3-го семестра Netology и их WebVTT-субтитры.

Скрипт использует авторизацию из ``.env`` только для чтения расписания и
карточек занятий. Сами субтитры сохраняются локально в ``data/video_subtitles``
и не добавляются в память наставника целиком.

Правила отбора:
* английский язык — только преподаватель STUDY_ENGLISH_TEACHER;
* дискретная математика — все лекции и только практика STUDY_DISCRETE_GROUP;
  (значения — в .env через study_profile.py, не в коде: репозиторий публичный)
* остальные курсы 3-го семестра — все элементы типа video/webinar.

Пример:
    python3 scripts/sync_netology_videos.py --extract-organization
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import html as html_lib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import NETOLOGY_EMAIL, NETOLOGY_PASSWORD
from study_profile import DISCRETE_GROUP, READING_GROUP, english_teacher_ok, matches_group


PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
SUBTITLE_DIR = DATA_DIR / "video_subtitles"
TRANSCRIPT_DIR = DATA_DIR / "video_transcripts"
MANIFEST_PATH = DATA_DIR / "video_manifest.json"

BASE_URL = "https://netology.ru"
PROFESSION_ID = 59683
SEMESTER_PREFIX = "3 семестр:"
VIDEO_TYPES = {"video", "webinar"}

VTT_RE = re.compile(r"https?://[^\"'<>\\]+?\.vtt(?:\?[^\"'<>\\]*)?", re.I)
SAFE_RE = re.compile(r"[^\wа-яА-ЯёЁ.-]+", re.UNICODE)


def _clean(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    return value


def _slug(value: str, max_len: int = 80) -> str:
    value = SAFE_RE.sub("_", _clean(value)).strip("._")
    return (value or "video")[:max_len]


def _date_from_title(title: str, starts_at: str | None) -> str | None:
    match = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", title or "")
    if match:
        return f"{match.group(3)}-{match.group(2)}-{match.group(1)}"
    if starts_at:
        return starts_at[:10]
    return None


def _selected(course: str, lesson_title: str, item_title: str) -> tuple[bool, str | None, str | None, str | None]:
    """Вернуть (включать, преподаватель, группа, тип занятия)."""
    title = f"{lesson_title} {item_title}"
    lower_course = course.lower()
    teacher = None
    teacher_match = re.search(r"Преподаватель\s+([^.]*)", title, re.I)
    if teacher_match:
        teacher = _clean(teacher_match.group(1))
    group = None
    group_match = re.search(r"(?:Группа\s+|ЛБ[- ]?)([0-9.]+)", title, re.I)
    if group_match:
        group = group_match.group(0).replace(" ", "")

    if "иностранный язык" in lower_course or "английский" in lower_course:
        return (english_teacher_ok(title), teacher, group, "lecture")

    if "дискретная математика" in lower_course:
        is_practice = "практик" in title.lower() or "лб-" in title.lower()
        if is_practice:
            mine = matches_group(DISCRETE_GROUP, title)
            return (mine, teacher, DISCRETE_GROUP if mine else group, "practice")
        return ("лекц" in title.lower(), teacher, group, "lecture")

    if "аналитическое чтение" in lower_course:
        # Без явного фильтра любая другая группа этого курса (их несколько
        # параллельных) попала бы в базу, как только появится её запись.
        mine = matches_group(READING_GROUP, title)
        return (mine, teacher, READING_GROUP if mine else group, "lecture")

    return True, teacher, group, "lecture"


def _extract_vtt_url(page: str) -> str | None:
    page = html_lib.unescape(page)
    candidates = VTT_RE.findall(page)
    for url in candidates:
        if url.lower().startswith("http"):
            return url
    return None


def _parse_vtt(raw: str) -> tuple[str, int]:
    """Убрать таймкоды/служебные строки, сохранив текст для поиска."""
    lines = []
    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line or line == "WEBVTT" or line.startswith("NOTE"):
            continue
        if "-->" in line or line.isdigit():
            continue
        line = re.sub(r"<[^>]+>", "", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line and (not lines or line != lines[-1]):
            lines.append(line)
    return "\n".join(lines), len(lines)


def _load_manifest() -> dict[str, Any]:
    if not MANIFEST_PATH.exists():
        return {"version": 1, "updated_at": None, "items": []}
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "updated_at": None, "items": []}


def _save_manifest(manifest: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    manifest["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    tmp = MANIFEST_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(MANIFEST_PATH)


async def _login(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/backend/api/user/sign_in",
        json={"login": NETOLOGY_EMAIL, "password": NETOLOGY_PASSWORD, "remember": True},
    )
    response.raise_for_status()
    if not client.cookies.get("_netology-on-rails_session"):
        raise RuntimeError("Netology не вернула сессию авторизации")


async def _collect_catalog(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    response = await client.get(f"/backend/api/user/professions/{PROFESSION_ID}/schedule")
    response.raise_for_status()
    modules = response.json().get("profession_modules", [])
    catalog: list[dict[str, Any]] = []
    for module in modules:
        program = module.get("program", {})
        program_id = program.get("id")
        if not program_id:
            continue
        schedule_response = await client.get(f"/backend/api/user/programs/{program_id}/schedule")
        if schedule_response.status_code != 200:
            continue
        schedule = schedule_response.json()
        course = schedule.get("title", "")
        if not course.startswith(SEMESTER_PREFIX):
            continue
        for lesson in schedule.get("lessons", []):
            lesson_title = _clean(lesson.get("title", ""))
            for item in lesson.get("lesson_items", []):
                if item.get("type") not in VIDEO_TYPES:
                    continue
                item_title = _clean(item.get("title", ""))
                include, teacher, group, lesson_type = _selected(course, lesson_title, item_title)
                if not include:
                    continue
                catalog.append({
                    "course": course.removeprefix(SEMESTER_PREFIX).strip(),
                    "course_full": course,
                    "program_id": program_id,
                    "lesson_id": lesson.get("id"),
                    "lesson_number": lesson.get("number"),
                    "lesson_title": lesson_title,
                    "item_id": item.get("id"),
                    "item_type": item.get("type"),
                    "item_title": item_title,
                    "starts_at": item.get("starts_at"),
                    "teacher": teacher,
                    "group": group,
                    "lesson_type": lesson_type,
                    "path": item.get("path"),
                })
    return catalog


async def _details(client: httpx.AsyncClient, item: dict[str, Any]) -> dict[str, Any]:
    response = await client.get(f"/backend/api/user/lesson_items/{item['item_id']}")
    if response.status_code != 200:
        return {}
    return response.json()


async def _download_item(
    client: httpx.AsyncClient,
    item: dict[str, Any],
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    async with semaphore:
        details = await _details(client, item)
        item = {**item, **{k: details.get(k) for k in ("video_url", "starts_at", "ends_at", "title") if k in details}}
        item["source_url"] = f"{BASE_URL}{item.get('path')}" if item.get("path") else None
        item["date"] = _date_from_title(item.get("item_title", ""), item.get("starts_at"))
        video_url = item.get("video_url")
        if not video_url:
            item["status"] = "no_recording"
            item["status_detail"] = "Карточка есть, но запись ещё не опубликована."
            return item

        try:
            player = await client.get(video_url.replace("https://kinescope.io", "https://kinescope.io"))
            player.raise_for_status()
            vtt_url = _extract_vtt_url(player.text)
            item["vtt_url"] = vtt_url
            if not vtt_url:
                item["status"] = "no_subtitles"
                item["status_detail"] = "Запись доступна, но VTT в метаданных плеера не найден."
                return item
            subtitle = await client.get(vtt_url)
            subtitle.raise_for_status()
            raw = subtitle.text
            filename = f"{_slug(item['course'])}__{item['item_id']}__{_slug(item['item_title'], 60)}.vtt"
            text_filename = filename[:-4] + ".txt"
            SUBTITLE_DIR.mkdir(parents=True, exist_ok=True)
            TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
            vtt_path = SUBTITLE_DIR / filename
            text_path = TRANSCRIPT_DIR / text_filename
            if not vtt_path.exists() or vtt_path.read_text(encoding="utf-8") != raw:
                vtt_path.write_text(raw, encoding="utf-8")
            transcript, line_count = _parse_vtt(raw)
            text_path.write_text(transcript + "\n", encoding="utf-8")
            item.update({
                "status": "downloaded",
                "subtitle_path": str(vtt_path.relative_to(PROJECT_DIR)),
                "transcript_path": str(text_path.relative_to(PROJECT_DIR)),
                "subtitle_bytes": len(raw.encode("utf-8")),
                "transcript_lines": line_count,
            })
        except Exception as error:  # сетевые ошибки не должны терять весь пакет
            item["status"] = "error"
            item["status_detail"] = f"{type(error).__name__}: {error}"
        return item


def _run_org_extraction(items: list[dict[str, Any]], limit: int | None = None) -> None:
    """Разбор новых вебинаров через Claude (scripts/analyze_webinar.py) —
    вся расшифровка целиком, без regex-предфильтра, см. его docstring.
    Только реально новые/непройденные (analyzed не выставлен мерджем выше в
    async_main) — иначе каждый 6-часовой тик заново гонял бы Клода по всем
    уже скачанным вебинарам.

    limit — сколько вебинаров разобрать за один запуск. При первом включении
    пайплайна разом накопился большой бэклог уже скачанных, но ни разу не
    разобранных записей (105+) — без ограничения один тик 6-часового цикла
    попытался бы прогнать их все сразу (часы работы, заметный расход токенов
    Sonnet). Свежие единичные вебинары так не тормозятся — им бэклог не
    мешает, они просто идут в очереди следующими."""
    script = PROJECT_DIR / "scripts" / "analyze_webinar.py"
    # Живая сессия с преподавателем — item_type="webinar" (Нетологии-родной
    # плеер: БД/УП/UX-UI/Архитектура/ПИР) ИЛИ item_type="video" с полем
    # "teacher" (иностранный язык/дискретная математика/аналитическое
    # чтение — там запись живого занятия почему-то тоже типизирована как
    # "video", отличить от обычного предзаписанного видеокурса можно только
    # по наличию преподавателя+даты в названии, см. _selected(); проверено:
    # ни один из 259 обычных видеокурсов teacher не проставляет, только эти
    # 8 записей живых занятий). Обычные видеоуроки по-прежнему скачиваются
    # и лежат в data/video_transcripts/ — mentor_qa.py их читает при
    # обучении по запросу, просто не разбираются здесь на организационное.
    pending = [
        item for item in items
        if item.get("status") == "downloaded"
        and (item.get("item_type") == "webinar" or item.get("teacher"))
        and not item.get("analyzed")
    ]
    if not pending:
        return
    total_pending = len(pending)
    if limit is not None:
        pending = pending[:limit]
    print(f"Новых вебинаров для разбора Клодом: {total_pending}"
          + (f" (в этом запуске: {len(pending)})" if limit is not None and total_pending > len(pending) else ""))
    for item in pending:
        print(f"  → {item['course']} — {item['item_title']}")
        result = subprocess.run(
            ["python3", str(script), "--item-id", str(item["item_id"])],
            cwd=PROJECT_DIR, check=False, capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"    ошибка разбора (returncode={result.returncode}): {(result.stderr or '')[-300:]}")


async def async_main(args: argparse.Namespace) -> int:
    timeout = httpx.Timeout(60.0, connect=30.0)
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=timeout, follow_redirects=True) as client:
        await _login(client)
        catalog = await _collect_catalog(client)
        semaphore = asyncio.Semaphore(args.concurrency)
        results = await asyncio.gather(*(_download_item(client, item, semaphore) for item in catalog))

    manifest = _load_manifest()
    by_id = {str(item.get("item_id")): item for item in manifest.get("items", [])}
    for item in results:
        key = str(item["item_id"])
        old = by_id.get(key)
        # _download_item строит item заново каждый прогон (из каталога, не из
        # манифеста) — без этого переноса каждый запуск стирал бы analyzed/
        # notes_path, и analyze_webinar.py --all-new разбирал бы одни и те же
        # вебинары заново на каждом 6-часовом тике.
        if old and old.get("analyzed"):
            if old.get("subtitle_bytes") == item.get("subtitle_bytes"):
                item["analyzed"] = old.get("analyzed")
                item["analyzed_at"] = old.get("analyzed_at")
                item["notes_path"] = old.get("notes_path")
            # иначе субтитры реально изменились — оставляем analyzed
            # неустановленным, чтобы разобрать заново
        by_id[key] = item
    manifest["items"] = sorted(by_id.values(), key=lambda x: (x.get("course", ""), x.get("date") or "9999", x.get("item_id", 0)))
    manifest["catalog_count"] = len(catalog)
    manifest["downloaded_count"] = sum(item.get("status") == "downloaded" for item in manifest["items"])
    manifest["no_recording_count"] = sum(item.get("status") == "no_recording" for item in manifest["items"])
    manifest["no_subtitles_count"] = sum(item.get("status") == "no_subtitles" for item in manifest["items"])
    manifest["error_count"] = sum(item.get("status") == "error" for item in manifest["items"])
    manifest["scope"] = "Доступные video/webinar 3-го семестра с фильтрами английского и дискретной математики"
    _save_manifest(manifest)

    if args.extract_organization:
        _run_org_extraction(results, limit=args.extract_limit)

    print(f"Каталог: {len(catalog)} элементов")
    print(f"Субтитры скачаны: {sum(x.get('status') == 'downloaded' for x in results)}")
    print(f"Без опубликованной записи: {sum(x.get('status') == 'no_recording' for x in results)}")
    print(f"Запись без VTT: {sum(x.get('status') == 'no_subtitles' for x in results)}")
    print(f"Ошибки: {sum(x.get('status') == 'error' for x in results)}")
    print(f"Индекс: {MANIFEST_PATH}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--concurrency", type=int, default=8, help="одновременных запросов к Netology/Kinescope")
    parser.add_argument("--extract-organization", action="store_true", help="разобрать новые вебинары Клодом на теорию/организацию")
    parser.add_argument("--extract-limit", type=int, default=6, help="сколько неразобранных вебинаров разбирать за один запуск (бэклог не мешает свежим — см. _run_org_extraction)")
    args = parser.parse_args()
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
