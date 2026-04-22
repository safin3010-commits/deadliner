import re
import json
import datetime
import httpx
from bs4 import BeautifulSoup
from config import MODEUS_USERNAME, MODEUS_PASSWORD, UFA_TZ
from storage import get_token, save_token

MODEUS_BASE_URL = "https://utmn.modeus.org"
MODEUS_CONFIG_URL = f"{MODEUS_BASE_URL}/schedule-calendar/assets/app.config.json"
MODEUS_EVENTS_URL = f"{MODEUS_BASE_URL}/schedule-calendar-v2/api/calendar/events/search"
SCHEDULE_CACHE_FILE = "data/schedule_cache.json"

_token_re = re.compile(r"id_token=([a-zA-Z0-9\-_.]+)")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

# ─── Кэш расписания ───────────────────────────────────────────────────

def _load_schedule_cache() -> dict:
    try:
        with open(SCHEDULE_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_schedule_cache(cache: dict):
    try:
        with open(SCHEDULE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Modeus cache save failed: {e}")


def _get_cached_week(week_start: datetime.date) -> dict | None:
    cache = _load_schedule_cache()
    entry = cache.get(week_start.isoformat())
    if not entry:
        return None
    cached_at = datetime.datetime.fromisoformat(entry["cached_at"])
    age_hours = (datetime.datetime.now(tz=datetime.UTC) - cached_at).total_seconds() / 3600
    if age_hours < 12:
        print(f"Modeus: кэш расписания (возраст {age_hours:.1f}ч)")
        return entry["data"]
    return None


def _set_cached_week(week_start: datetime.date, data: dict):
    cache = _load_schedule_cache()
    cache[week_start.isoformat()] = {
        "cached_at": datetime.datetime.now(tz=datetime.UTC).isoformat(),
        "data": data,
    }
    if len(cache) > 8:
        del cache[sorted(cache.keys())[0]]
    _save_schedule_cache(cache)


# ─── Авторизация ──────────────────────────────────────────────────────

async def _try_auth(http2: bool) -> str | None:
    from secrets import token_hex
    from urllib.parse import urlparse

    async with httpx.AsyncClient(timeout=60, follow_redirects=True, http2=http2, headers=HEADERS) as s:
        r = await s.get(MODEUS_CONFIG_URL)
        r.raise_for_status()
        config = r.json()
        client_id = config["wso"]["clientId"]
        auth_url = config["wso"]["loginUrl"]

        r = await s.get(auth_url, params={
            "client_id": client_id,
            "redirect_uri": MODEUS_BASE_URL + "/",
            "response_type": "id_token",
            "scope": "openid",
            "nonce": token_hex(16),
            "state": token_hex(16),
        })

        if r.status_code == 403:
            return None

        html = BeautifulSoup(r.text, "lxml")
        form = html.find("form")
        if not form:
            return None

        form_action = form.get("action", str(r.url))
        if form_action.startswith("/"):
            p = urlparse(str(r.url))
            form_action = f"{p.scheme}://{p.netloc}{form_action}"

        login_data = {"UserName": MODEUS_USERNAME, "Password": MODEUS_PASSWORD, "AuthMethod": "FormsAuthentication"}
        for inp in form.find_all("input", type="hidden"):
            if inp.get("name"):
                login_data[inp["name"]] = inp.get("value", "")

        r = await s.post(form_action, data=login_data)

        html2 = BeautifulSoup(r.text, "lxml")
        form2 = html2.find("form")
        inputs2 = [i.get("name") for i in form2.find_all("input")] if form2 else []

        if not form2 or ("UserName" in inputs2 and "Password" in inputs2):
            return None

        saml_action = form2.get("action", "https://auth.modeus.org/commonauth")
        if saml_action.startswith("/"):
            p = urlparse(str(r.url))
            saml_action = f"{p.scheme}://{p.netloc}{saml_action}"

        saml_data = {}
        for inp in form2.find_all("input"):
            if inp.get("name"):
                saml_data[inp["name"]] = inp.get("value", "")

        r2 = await s.post(saml_action, data=saml_data, follow_redirects=False)
        loc = r2.headers.get("Location", "")

        token = _token_re.search(loc)
        if token:
            return token.group(1)

        current = loc
        for _ in range(8):
            if not current:
                break
            r_step = await s.get(current, follow_redirects=False)
            next_loc = r_step.headers.get("Location", "")
            token = _token_re.search(next_loc)
            if token:
                return token.group(1)
            token = _token_re.search(str(r_step.url))
            if token:
                return token.group(1)
            if not next_loc or next_loc == current:
                break
            current = next_loc

        return None


async def get_modeus_jwt() -> str | None:
    for use_http2 in [True, False]:
        try:
            token = await _try_auth(use_http2)
            if token:
                save_token("modeus_jwt", token)
                save_token(
                    "modeus_jwt_expires",
                    (datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(hours=11)).isoformat()
                )
                print("Modeus: авторизация успешна ✅")
                return token
        except Exception as e:
            print(f"Modeus auth ({'http2' if use_http2 else 'http1'}) failed: {e}")
    print("Modeus: не удалось авторизоваться")
    return None


async def get_cached_jwt() -> str | None:
    token = get_token("modeus_jwt")
    expires_str = get_token("modeus_jwt_expires")
    if token and expires_str:
        expires = datetime.datetime.fromisoformat(expires_str)
        if datetime.datetime.now(tz=datetime.UTC) < expires:
            print("Modeus: используем кэшированный токен")
            return token
    print("Modeus: токен истёк, авторизуемся...")
    return await get_modeus_jwt()


def get_person_id_from_jwt(jwt_token: str) -> str | None:
    try:
        import jwt
        decoded = jwt.decode(jwt_token, options={"verify_signature": False})
        return decoded.get("person_id")
    except Exception as e:
        print(f"Modeus: не удалось декодировать JWT: {e}")
        return None


# ─── Получение расписания ─────────────────────────────────────────────

async def get_schedule(jwt_token: str, person_id: str, date: datetime.date) -> list:
    try:
        utc = datetime.UTC
        time_min = datetime.datetime.combine(date, datetime.time.min, tzinfo=utc).isoformat()
        time_max = datetime.datetime.combine(date, datetime.time.max.replace(microsecond=0), tzinfo=utc).isoformat()

        payload = {"timeMin": time_min, "timeMax": time_max, "attendeePersonId": [person_id], "size": 50}

        async with httpx.AsyncClient(base_url=MODEUS_BASE_URL, timeout=30, http2=True) as client:
            client.headers["Authorization"] = f"Bearer {jwt_token}"
            client.headers["Content-Type"] = "application/json"
            response = await client.post(MODEUS_EVENTS_URL, json=payload)

            if response.status_code == 401:
                print("Modeus: токен истёк при запросе расписания")
                return []

            response.raise_for_status()
            return parse_schedule(response.json())

    except Exception as e:
        print(f"Modeus get_schedule failed: {e}")
        return []


async def get_week_schedule(week_start: datetime.date) -> dict:
    cached = _get_cached_week(week_start)
    if cached is not None:
        return cached

    print(f"Modeus: запрашиваем неделю с {week_start}...")
    jwt_token = await get_cached_jwt()
    if not jwt_token:
        return {}

    person_id = get_person_id_from_jwt(jwt_token)
    if not person_id:
        return {}

    schedule_by_day = {}
    for i in range(7):
        day = week_start + datetime.timedelta(days=i)
        schedule_by_day[day.isoformat()] = await get_schedule(jwt_token, person_id, day)

    _set_cached_week(week_start, schedule_by_day)
    return schedule_by_day


def _get_week_start(offset_weeks: int = 0) -> datetime.date:
    today = datetime.datetime.now(tz=UFA_TZ).date()
    monday = today - datetime.timedelta(days=today.weekday())
    return monday + datetime.timedelta(weeks=offset_weeks)


def parse_schedule(data: dict) -> list:
    embedded = data.get("_embedded", {})
    events = embedded.get("events", [])
    locations = embedded.get("event-locations", [])
    courses = embedded.get("course-unit-realizations", [])

    locations_by_event = {
        loc["eventId"]: loc.get("customLocation", "")
        for loc in locations
    }
    courses_by_id = {
        c["id"]: c.get("name", "Неизвестный предмет")
        for c in courses
    }

    schedule = []
    for event in events:
        event_id = event.get("id")
        name = event.get("name", "Без названия")
        start = event.get("start")
        end = event.get("end")
        # description содержит тему занятия, например "Лекционное занятие 5"
        description = event.get("description", "")

        links = event.get("_links", {})
        course_href = links.get("course-unit-realization", {}).get("href", "").strip("/")
        course_name = courses_by_id.get(course_href, name)
        location = locations_by_event.get(event_id, "")

        try:
            start_dt = datetime.datetime.fromisoformat(start).astimezone(UFA_TZ)
            end_dt = datetime.datetime.fromisoformat(end).astimezone(UFA_TZ)
        except Exception:
            continue

        schedule.append({
            "id": event_id,
            "name": name,
            "course_name": course_name,
            "description": description,
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "start_time": start_dt.strftime("%H:%M"),
            "end_time": end_dt.strftime("%H:%M"),
            "location": location,
        })

    schedule.sort(key=lambda x: x["start"])
    return schedule


async def fetch_schedule_today() -> list:
    print("Modeus: получаем расписание на сегодня...")
    today = datetime.datetime.now(tz=UFA_TZ).date()
    week_start = today - datetime.timedelta(days=today.weekday())
    week_data = await get_week_schedule(week_start)
    return week_data.get(today.isoformat(), [])
