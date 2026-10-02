"""
Яндекс Календарь группы — официальный источник ссылок на занятия.

Объявление группы в ВК (29.09.2026): «с 15 сентября все ссылки только в
Яндекс Календаре, в ВК больше не дублируем»; в yetanothercalendar ссылки
«не подтянутся». Публичная встраиваемая страница календаря сама грузит
события JSON-запросом — им и пользуемся (без входа и без браузера).

В календаре пары ВСЕХ групп потока (несколько групп английского,
дискретки, чтения) — оставляем только свои: номера групп и преподаватель
английского — в .env (study_profile.py: STUDY_GROUPS, STUDY_ENGLISH_TEACHER),
не в коде (репозиторий публичный). События без «Группа …» (вебинары
Нетологии 🔷, асинхронные LXP 🔵) общие — берём все.

Результат: data/group_calendar.json — эта и следующая неделя.
"""
from __future__ import annotations

import datetime
import json
import os
import re

import httpx

from config import UFA_TZ

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_FILE = os.path.join(PROJECT_DIR, "data", "group_calendar.json")
LAYER_ID = os.getenv("GROUP_CALENDAR_LAYER_ID", "38253744")
API = "https://calendar.360.yandex.ru/embed/models?_models=get-events&withTasks=1"
LINK_RE = re.compile(r"https://my\.mts-link\.ru/\S+|https://\S*(?:zoom|telemost|meet)\S+")


def _norm(s: str) -> str:
    return re.sub(r"[\s\-.]", "", (s or "").lower())


def is_mine(name: str) -> bool:
    from study_profile import ENGLISH_TEACHER, study_groups
    m = re.search(r"Группа\s+([^\s.]+(?:\.\d+)?)", name)
    if not m:
        return True                     # общие события (Нетология, LXP)
    group = _norm(m.group(1))
    if "ин.яз" in name.lower() or "иностран" in name.lower():
        return bool(ENGLISH_TEACHER) and ENGLISH_TEACHER.lower() in name.lower()
    return any(group == _norm(g) for g in study_groups())


def fetch(date_from: datetime.date, date_to: datetime.date) -> list[dict]:
    body = {"models": [{"name": "get-events", "params": {
        "layerId": [LAYER_ID], "limitAttendees": True, "showDeclined": False,
        "from": date_from.isoformat(), "to": date_to.isoformat(), "tz": "Asia/Yekaterinburg"}}]}
    # Разовые обрывы TLS (VPN на машине) — до трёх попыток с паузой.
    import time
    for attempt in range(3):
        try:
            r = httpx.post(API, json=body, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            r.raise_for_status()
            break
        except (httpx.TransportError, httpx.HTTPStatusError):
            if attempt == 2:
                raise
            time.sleep(3 * (attempt + 1))
    model = r.json()["models"][0]
    if model.get("status") != "ok":
        raise RuntimeError(f"календарь ответил ошибкой: {model.get('error')}")
    out = []
    for e in model["data"]["events"]:
        name = (e.get("name") or "").strip()
        if not is_mine(name):
            continue
        # instanceStartTs/endTs приходят в UTC без пояса.
        start = datetime.datetime.fromisoformat(e["instanceStartTs"]).replace(tzinfo=datetime.timezone.utc)
        # Длительность — из startTs/endTs (у повторяющихся они от первого
        # вхождения, поэтому прибавляем к instanceStartTs, а не берём endTs).
        try:
            dur = datetime.datetime.fromisoformat(e["endTs"]) - datetime.datetime.fromisoformat(e["startTs"])
        except (KeyError, TypeError, ValueError):
            dur = datetime.timedelta(minutes=90)
        end = start + dur
        desc = (e.get("description") or "").strip()
        out.append({
            "name": re.sub(r"^[^\wА-Яа-яЁё]+", "", name).strip(),
            "start": start.astimezone(UFA_TZ).isoformat(),
            "end": end.astimezone(UFA_TZ).isoformat(),
            "all_day": bool(e.get("isAllDay")),
            "links": LINK_RE.findall(desc),
            "note": re.sub(r"\s+", " ", LINK_RE.sub("", desc)).strip()[:200],
        })
    return out


def refresh() -> int:
    """Эта и следующая неделя → data/group_calendar.json. Ошибку пробрасывает
    наверх — вызывающий решает, логировать ли (старый файл не трогаем)."""
    from data_lock import atomic_write_json
    today = datetime.datetime.now(tz=UFA_TZ).date()
    monday = today - datetime.timedelta(days=today.weekday())
    events = fetch(monday, monday + datetime.timedelta(days=13))
    atomic_write_json(OUT_FILE, {"fetched_at": datetime.datetime.now(tz=UFA_TZ).isoformat(), "events": events})
    return len(events)


if __name__ == "__main__":
    print(refresh())
