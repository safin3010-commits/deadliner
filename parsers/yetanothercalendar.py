"""
Сторонний агрегатор Modeus+Netology+LMS — https://yetanothercalendar.ru
(github.com/depocoder/YetAnotherCalendar). Используем ТОЛЬКО за двумя вещами,
которых нет в наших собственных данных:
1. Реальные ссылки на вебинары (my.mts-link.ru) — у пар Модеуса своих ссылок нет.
2. Перекрёстная проверка нашего расписания (у Modeus-событий там есть явный
   флаг is_lxp, хотя в интерфейсе сайта он не показывается).

Логин пользователя (Модеус/Нетология) НИКОГДА не проходит через наш код —
только через сохранённую браузерную сессию (куки, включая httpOnly, +
localStorage с зашифрованным на клиенте "vault"), которую пользователь один
раз заводит сам через save_session(), точно как save_cookies() в vk_browser.py.
"""
import asyncio
import datetime
import json
import os
from config import UFA_TZ, CHROME_PATH

YAC_BASE_URL = "https://yetanothercalendar.ru"
YAC_STORAGE_FILE = "data/yac_storage_state.json"


async def save_session(chrome_path: str | None = None):
    """Интерактивно — открываем видимый браузер, пользователь логинится сам
    (Нетология + Модеус), жмём Enter здесь после того как увидим календарь.
    Запуск вручную: venv/bin/python3 -m parsers.yetanothercalendar --save-session"""
    from playwright.async_api import async_playwright
    _chrome = chrome_path or CHROME_PATH or None
    async with async_playwright() as p:
        launch = {"headless": False}
        if _chrome:
            launch["executable_path"] = _chrome
        browser = await p.chromium.launch(**launch)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(YAC_BASE_URL)
        print("Войди в свой аккаунт (Нетология + Модеус) в открывшемся окне.")
        print("Когда увидишь свой календарь — нажми Enter здесь...")
        input()
        os.makedirs("data", exist_ok=True)
        await context.storage_state(path=YAC_STORAGE_FILE)
        print(f"Сессия сохранена в {YAC_STORAGE_FILE}")
        await browser.close()


async def fetch_week_events(week_start: datetime.date | None = None) -> dict | None:
    """Возвращает {"netology_webinars": [...], "modeus_events": [...]} для
    недели, как её видит yetanothercalendar.ru — с реальными ссылками на
    вебинары и явным is_lxp у пар Модеуса. None при отсутствующей/протухшей
    сессии или сетевой ошибке — вызывающий код должен тихо деградировать,
    а не считать это "пар нет".

    week_start сейчас не используется для навигации — берём ту неделю,
    которую сайт показывает по умолчанию при открытии (обычно текущая),
    т.к. UI-навигация по неделям через API не тестировалась. Достаточно для
    задачи "сегодня/завтра", т.к. они почти всегда попадают в эту неделю."""
    if not os.path.exists(YAC_STORAGE_FILE):
        print("YAC: сессия не сохранена — запусти save_session()")
        return None

    from playwright.async_api import async_playwright

    bulk_response: dict = {}
    mts_links: dict = {}

    async def on_response(response):
        nonlocal bulk_response, mts_links
        url = response.url
        try:
            if "/api/bulk/events/" in url and response.status == 200:
                bulk_response = await response.json()
            elif "/api/mts/links" in url and response.status == 200:
                data = await response.json()
                mts_links.update(data.get("links", {}))
        except Exception:
            pass

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = await browser.new_context(storage_state=YAC_STORAGE_FILE)
            page = await context.new_page()
            page.on("response", on_response)
            await page.goto(YAC_BASE_URL, timeout=30000, wait_until="domcontentloaded")
            # Ждём собственный auto-логин сайта (шлёт сохранённые в vault
            # токены на /api/netology/auth, /api/lms/auth, /api/modeus/person-id/)
            # и последующий /api/bulk/events/ — это несколько сетевых кругов подряд.
            try:
                await page.wait_for_selector("text=Мое расписание", timeout=20000)
            except Exception:
                pass
            await page.wait_for_timeout(4000)

            if "login" in page.url.lower():
                print("YAC: сессия протухла — нужен новый save_session()")
                await browser.close()
                return None

            await browser.close()
    except Exception as e:
        print(f"YAC fetch error: {e!r}")
        return None

    if not bulk_response:
        print("YAC: не удалось получить /api/bulk/events/ (сессия протухла или сайт недоступен)")
        return None

    netology = bulk_response.get("netology", {}) or {}
    utmn = bulk_response.get("utmn", {}) or {}
    modeus_events = utmn.get("modeus_events", []) or []
    for ev in modeus_events:
        ev["mts_link"] = mts_links.get(ev.get("id"))

    return {
        "netology_webinars": netology.get("webinars", []) or [],
        "modeus_events": modeus_events,
    }


def to_day_schedule(yac_result: dict, day: datetime.date) -> list:
    """Переводит fetch_week_events() в тот же формат, что parse_schedule()
    (parsers/modeus.py) и netology.py отдают для конкретного дня — чтобы
    scheduler.py мог использовать yetanothercalendar как настоящий резервный
    источник (а не только для сверки/ссылок, как раньше), когда и Modeus,
    и Нетология сами по себе недоступны.

    2026-09-22: раньше этого перевода не было вообще — yetanothercalendar
    участвовал только в проверке текста наставника и подстановке ссылок на
    вебинары, реальным резервом при сбое основных источников не был, хотя
    это обсуждалось как задача."""
    if not yac_result:
        return []
    day_iso = day.isoformat()
    out = []

    for ev in yac_result.get("modeus_events", []) or []:
        start = ev.get("start") or ""
        if not start.startswith(day_iso):
            continue
        try:
            start_dt = datetime.datetime.fromisoformat(start).astimezone(UFA_TZ)
            end_dt = datetime.datetime.fromisoformat(ev.get("end") or start).astimezone(UFA_TZ)
        except Exception:
            continue
        location = ev.get("customLocation") or ""
        if ev.get("is_lxp") and "lxp" not in location.lower():
            location = (location + " LXP").strip()
        out.append({
            "id": ev.get("id"),
            "name": ev.get("name", "Без названия"),
            "course_name": ev.get("course_name") or ev.get("name", "Без названия"),
            "description": ev.get("nameShort") or "",
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "start_time": start_dt.strftime("%H:%M"),
            "end_time": end_dt.strftime("%H:%M"),
            "location": location,
        })

    for w in yac_result.get("netology_webinars", []) or []:
        starts_at = w.get("starts_at") or ""
        if not starts_at.startswith(day_iso):
            continue
        try:
            start_dt = datetime.datetime.fromisoformat(starts_at).astimezone(UFA_TZ)
            end_dt = datetime.datetime.fromisoformat(w.get("ends_at") or starts_at).astimezone(UFA_TZ)
        except Exception:
            continue
        out.append({
            "id": f"yac_netology_webinar_{w.get('id')}",
            "name": w.get("title", "Вебинар"),
            "course_name": w.get("block_title") or w.get("title", "Вебинар"),
            "description": "Вебинар",
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "start_time": start_dt.strftime("%H:%M"),
            "end_time": end_dt.strftime("%H:%M"),
            "location": w.get("webinar_url") or "",
            "source": "netology",
        })

    out.sort(key=lambda x: x["start"])
    return out


if __name__ == "__main__":
    import sys
    if "--save-session" in sys.argv:
        asyncio.run(save_session())
    else:
        async def _test():
            result = await fetch_week_events()
            if result is None:
                print("Нет данных")
                return
            print(f"Нетология вебинаров: {len(result['netology_webinars'])}")
            print(f"Модеус событий: {len(result['modeus_events'])}")
            for ev in result["modeus_events"][:5]:
                print(f"  {ev.get('start')} — {ev.get('course_name')} (is_lxp={ev.get('is_lxp')}, mts_link={ev.get('mts_link')})")
        asyncio.run(_test())
