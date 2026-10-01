"""
Вариант export_netology_syllabus.py для выгрузки 1 и 2 семестра (не 3).
Сгенерировано для повторения старого материала — logика 1-в-1 скопирована
из оригинального скрипта, изменён только фильтр по семестру и папка вывода.

Запуск: venv/bin/python3 scripts/export_netology_syllabus_1_2.py
"""
import asyncio
import datetime
import io
import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from bs4 import BeautifulSoup
import pdfplumber

from config import NETOLOGY_EMAIL, NETOLOGY_PASSWORD

BASE = "https://netology.ru"
MAIN_PROGRAM_ID = 59683
SEMESTER_PREFIXES = ("1 семестр:", "2 семестр:")
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "netology_conspekty_1_2")

TYPE_LABEL = {
    "video": "🎬 Видео",
    "text": "📄 Текст",
    "attachment": "📎 Материал",
    "webinar": "🎙 Вебинар",
    "task": "📝 Задание",
    "test": "✅ Тест",
    "quiz": "❓ Квиз",
}


def html_to_text(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    text = soup.get_text("\n")
    lines = [l.strip() for l in text.split("\n")]
    lines = [l for l in lines if l]
    return "\n".join(lines)


def extract_pdf_text(content: bytes) -> str:
    try:
        parts = []
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                t = page.extract_text() or ""
                if t.strip():
                    parts.append(t.strip())
        return "\n\n".join(parts)
    except Exception as e:
        return f"[не удалось извлечь текст PDF: {e}]"


async def auth(s: httpx.AsyncClient) -> str | None:
    r = await s.post(f"{BASE}/backend/api/user/sign_in", json={
        "login": NETOLOGY_EMAIL, "password": NETOLOGY_PASSWORD, "remember": True,
    })
    r.raise_for_status()
    return s.cookies.get("_netology-on-rails_session")


async def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    async with httpx.AsyncClient(base_url=BASE, timeout=30, follow_redirects=True) as s:
        cookie = await auth(s)
        if not cookie:
            print("Netology: логин не удался")
            return

    async with httpx.AsyncClient(
        base_url=BASE, timeout=30, follow_redirects=True,
        cookies={"_netology-on-rails_session": cookie},
    ) as s:
        r = await s.get(f"/backend/api/user/professions/{MAIN_PROGRAM_ID}/schedule")
        r.raise_for_status()
        modules = r.json().get("profession_modules", [])
        program_ids = [m.get("program", {}).get("id") for m in modules if m.get("program", {}).get("id")]

        courses = []
        for pid in program_ids:
            rr = await s.get(f"/backend/api/user/programs/{pid}/schedule")
            if rr.status_code != 200:
                continue
            d = rr.json()
            if d.get("title", "").startswith(SEMESTER_PREFIXES):
                courses.append(d)

        print(f"Курсов 1-2 семестра найдено: {len(courses)}")

        index_lines = [f"# Нетология — 1-2 семестр — сводное расписание\n", f"Сформировано: {datetime.datetime.now().strftime('%d.%m.%Y %H:%M')}\n"]

        for course in courses:
            title = course.get("title", "Без названия")
            safe_name = re.sub(r'[\\/*?:"<>|]', "", title).strip()
            print(f"\n=== {title} ===")

            lines = [f"# {title}\n"]
            deadlines = []
            webinars = []
            lesson_blocks = []

            for lesson in course.get("lessons", []):
                lesson_title = lesson.get("title", "")
                items = lesson.get("lesson_items", [])
                if not items:
                    continue

                block = [f"\n## Занятие {lesson.get('number', '?')}: {lesson_title}\n"]

                for item in items:
                    item_type = item.get("type", "")
                    item_title = item.get("title", "")
                    label = TYPE_LABEL.get(item_type, f"[{item_type}]")

                    try:
                        rd = await s.get(f"/backend/api/user/lesson_items/{item['id']}")
                        detail = rd.json() if rd.status_code == 200 else {}
                    except Exception as e:
                        detail = {}
                        print(f"  ! ошибка получения {item.get('id')}: {e}")

                    if item_type == "webinar":
                        starts_at = detail.get("starts_at") or item.get("starts_at")
                        when = ""
                        if starts_at:
                            try:
                                dt = datetime.datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
                                when = dt.strftime("%d.%m.%Y %H:%M")
                                webinars.append(f"{title} — {item_title} — {when}")
                            except Exception:
                                pass
                        block.append(f"{label}: {item_title}" + (f" ({when})" if when else ""))

                    elif item_type in ("task", "test", "quiz"):
                        block.append(f"{label}: {item_title}")
                        m = re.search(r"(?:дедлайн|до)\s*[—\-:]?\s*(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})", item_title, re.IGNORECASE)
                        if m:
                            deadlines.append(f"{title} — {item_title}")

                    elif item_type == "text":
                        content = html_to_text(detail.get("content", ""))
                        block.append(f"{label}: {item_title}")
                        if content:
                            block.append(content)
                        block.append("")

                    elif item_type == "attachment":
                        block.append(f"{label}: {item_title}")
                        files = detail.get("files", [])
                        for f in files:
                            ext = (f.get("extension") or "").lower()
                            fname = f.get("name", item_title)
                            link = f.get("link")
                            if ext == "pdf" and link:
                                try:
                                    rf = await s.get(link)
                                    if rf.status_code == 200:
                                        text = extract_pdf_text(rf.content)
                                        block.append(f"--- Содержимое «{fname}» ({f.get('size','')}) ---")
                                        block.append(text if text.strip() else "[пустой текстовый слой в PDF]")
                                        block.append("--- конец материала ---")
                                    else:
                                        block.append(f"[не удалось скачать {fname}: HTTP {rf.status_code}]")
                                except Exception as e:
                                    block.append(f"[ошибка скачивания {fname}: {e}]")
                            else:
                                block.append(f"[файл {fname} ({ext}) — не текстовый формат, ссылка: {link}]")
                        block.append("")

                    elif item_type == "video":
                        block.append(f"{label}: {item_title}")

                    else:
                        block.append(f"{label}: {item_title}")

                lesson_blocks.append("\n".join(block))
                print(f"  занятие «{lesson_title[:40]}» — {len(items)} элементов обработано")

            lines.append("\n".join(lesson_blocks))

            if deadlines:
                lines.insert(1, "\n## Дедлайны\n" + "\n".join(f"- {d}" for d in deadlines) + "\n")
            if webinars:
                insert_at = 2 if deadlines else 1
                lines.insert(insert_at, "\n## Вебинары\n" + "\n".join(f"- {w}" for w in webinars) + "\n")

            out_path = os.path.join(OUT_DIR, f"{safe_name}.txt")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            print(f"  -> сохранено: {out_path}")

            index_lines.append(f"\n## {title}")
            if deadlines:
                index_lines.append("Дедлайны:")
                index_lines.extend(f"- {d}" for d in deadlines)
            if webinars:
                index_lines.append("Вебинары:")
                index_lines.extend(f"- {w}" for w in webinars)
            index_lines.append(f"Файл: {safe_name}.txt")

        with open(os.path.join(OUT_DIR, "00_INDEX.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(index_lines))

        print(f"\nГотово. Файлы в {OUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
