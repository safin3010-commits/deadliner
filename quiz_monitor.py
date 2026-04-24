"""
Мониторинг тестов в Safari.
Запуск: python3 quiz_monitor.py
Остановка: Ctrl+C

Открываешь тест в Safari — бот забирает cookies из Safari и парсит все вопросы.
"""
import asyncio
import re
import subprocess
import httpx
from config import MY_TELEGRAM_ID, TELEGRAM_TOKEN

LMS_BASE_URL = "https://lms.utmn.ru"
PROXY = "http://127.0.0.1:10808"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
}

_sent_attempts: set = set()


def get_safari_url() -> str | None:
    try:
        result = subprocess.run(
            ["osascript", "-e", 'tell application "Google Chrome" to return URL of active tab of front window'],
            capture_output=True, text=True, timeout=3
        )
        url = result.stdout.strip()
        return url if url and url.startswith("http") else None
    except Exception:
        return None


def get_safari_cookies() -> dict:
    """Получаем cookies LMS из Chrome (кроссплатформенно)."""
    try:
        import browser_cookie3
        jar = browser_cookie3.chrome(domain_name="lms.utmn.ru")
        return {c.name: c.value for c in jar}
    except Exception as e:
        print(f"cookies error: {e}")
        return {}


def _clean(html: str) -> str:
    text = re.sub(r'<span[^>]*class="[^"]*nolink[^"]*"[^>]*>(.*?)</span>', r'\1', html, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&#\d+;', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _parse_questions(html: str) -> list[dict]:
    questions = []
    parts = re.split(r'(?=<div id="question-\d+-\d+")', html)

    for part in parts:
        if 'class="que ' not in part:
            continue

        num_m = re.search(r'<span class="qno">(\d+)</span>', part)
        num = int(num_m.group(1)) if num_m else len(questions) + 1

        qt = re.search(r'class="qtext"[^>]*>(.*?)</div>', part, re.DOTALL)
        if not qt:
            # Запасной — берём весь formulation блок
            qt = re.search(r'class="formulation[^"]*"[^>]*>(.*?)<div class="answer"', part, re.DOTALL)
        if not qt:
            continue
        q_text = _clean(qt.group(1))
        if not q_text or len(q_text) < 3:
            continue

        answers = []
        answer_divs = re.findall(r'<div class="r\d+">(.*?)\s*</div>\s*</div>', part, re.DOTALL)
        for adiv in answer_divs:
            letter_m = re.search(r'<span class="answernumber">([^<]+)</span>', adiv)
            letter = letter_m.group(1).strip() if letter_m else ""
            ans_m = re.search(r'class="flex-fill[^"]*"[^>]*>(.*?)</div>', adiv, re.DOTALL)
            ans_text = _clean(ans_m.group(1)) if ans_m else _clean(adiv)
            ans_text = re.sub(r'^[a-zA-Z]\. ', '', ans_text).strip()
            if ans_text:
                answers.append(f"{letter} {ans_text}".strip())

        questions.append({"num": num, "text": q_text[:800], "answers": answers[:8]})

    return questions


async def send_tg(text: str):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": MY_TELEGRAM_ID, "text": text, "parse_mode": "Markdown"}
            )
    except Exception as e:
        print(f"TG error: {e}")


async def fetch_page_with_safari_cookies(url: str) -> str | None:
    """Загружаем страницу используя cookies из Safari — та же сессия."""
    cookies = get_safari_cookies()
    if not cookies:
        print("⚠️ Не удалось получить cookies из Safari")
        return None

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=HEADERS, cookies=cookies, proxy=PROXY) as client:
            r = await client.get(url)
            if "login" in str(r.url).lower():
                return None
            return r.text
    except Exception as e:
        print(f"Fetch error: {e}")
        return None


async def fetch_next_page(attempt_id: str, cmid: str, from_page: int, sesskey: str, slots: str, cookies: dict) -> str | None:
    """POST на processattempt с cookies Safari."""
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=HEADERS, cookies=cookies, proxy=PROXY) as client:
            r = await client.post(
                f"{LMS_BASE_URL}/mod/quiz/processattempt.php?cmid={cmid}",
                data={
                    "attempt": attempt_id,
                    "thispage": str(from_page),
                    "nextpage": str(from_page + 1),
                    "timeup": "0",
                    "sesskey": sesskey,
                    "slots": slots,
                    "mdlscrollto": "",
                    "next": "Следующая страница",
                },
                headers={**HEADERS,
                         "Content-Type": "application/x-www-form-urlencoded",
                         "Referer": f"{LMS_BASE_URL}/mod/quiz/attempt.php?attempt={attempt_id}&cmid={cmid}&page={from_page}"}
            )
            if "login" in str(r.url).lower():
                return None
            return r.text
    except Exception as e:
        print(f"POST error: {e}")
        return None


async def process_quiz(url: str):
    attempt_m = re.search(r'attempt=(\d+)', url)
    cmid_m = re.search(r'cmid=(\d+)', url)
    attempt_id = attempt_m.group(1) if attempt_m else None
    cmid = cmid_m.group(1) if cmid_m else None

    if not attempt_id or attempt_id in _sent_attempts:
        return

    _sent_attempts.add(attempt_id)
    print(f"📝 Тест attempt={attempt_id}, cmid={cmid}")

    # Берём cookies прямо из Safari — та же авторизованная сессия
    cookies = get_safari_cookies()
    if not cookies:
        await send_tg("❌ Не удалось получить сессию из Safari. Убедись что Safari открыт и ты залогинен в LMS.")
        return

    # Загружаем страницу 0
    html = await fetch_page_with_safari_cookies(
        f"{LMS_BASE_URL}/mod/quiz/attempt.php?attempt={attempt_id}&cmid={cmid}&page=0"
    )
    if not html:
        await send_tg("❌ Не удалось загрузить тест. Возможно сессия истекла — обнови страницу в Safari.")
        _sent_attempts.discard(attempt_id)
        return

    # Название теста
    title_m = re.search(r'<title>([^<]+)</title>', html)
    quiz_title = _clean(title_m.group(1)) if title_m else "Тест"
    quiz_title = re.sub(r'\s*[|:]\s*.*$', '', quiz_title).strip()

    # Считаем страницы
    nav_pages = re.findall(r'page=(\d+)#', html)
    total_pages = max([int(p) for p in nav_pages], default=0) + 1
    print(f"Страниц: {total_pages}, название: {quiz_title}")

    await send_tg(f"📝 *{quiz_title}*\n📊 Всего вопросов: ~{total_pages}\n⏳ Парсю...")

    all_questions = []
    current_html = html

    for page_num in range(total_pages):
        if page_num > 0:
            sesskey_m = re.search(r'name="sesskey"\s+value="([^"]+)"', current_html)
            slots_m = re.search(r'name="slots"\s+value="([^"]*)"', current_html)
            sesskey = sesskey_m.group(1) if sesskey_m else ""
            slots = slots_m.group(1) if slots_m else ""

            current_html = await fetch_next_page(attempt_id, cmid, page_num - 1, sesskey, slots, cookies)
            if not current_html:
                print(f"⚠️ Страница {page_num+1} не загрузилась")
                break
            await asyncio.sleep(0.5)

        questions = _parse_questions(current_html)
        all_questions.extend(questions)
        print(f"  Страница {page_num+1}: {len(questions)} вопросов")

    if not all_questions:
        await send_tg("⚠️ Вопросы не найдены. Попробуй открыть тест заново.")
        return

    for q in all_questions:
        lines = [f"❓ *Вопрос {q['num']}*\n\n{q['text']}"]
        if q["answers"]:
            lines.append("\n*Варианты:*")
            for ans in q["answers"]:
                lines.append(f"  {ans}")
        await send_tg("\n".join(lines))
        await asyncio.sleep(0.3)

    await send_tg(f"✅ *Готово!* Все {len(all_questions)} вопросов отправлены")
    print(f"✅ Готово — {len(all_questions)} вопросов")


async def monitor():
    print("🔍 Мониторинг Safari запущен")
    print("   Открывай тесты в Safari — вопросы придут в Telegram")
    print("   Ctrl+C для остановки\n")

    await send_tg("🔍 *Мониторинг тестов запущен*\nОткрывай тест в Safari — вопросы пришлю сюда")

    last_url = None
    try:
        while True:
            url = get_safari_url()
            if url and url != last_url:
                last_url = url
                if "mod/quiz/attempt.php" in url and LMS_BASE_URL in url:
                    attempt_m = re.search(r'attempt=(\d+)', url)
                    attempt_id = attempt_m.group(1) if attempt_m else url
                    if attempt_id not in _sent_attempts:
                        await process_quiz(url)
            await asyncio.sleep(2)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n⏹ Остановлен")
        try:
            await send_tg("⏹ Мониторинг остановлен")
        except Exception:
            pass


asyncio.run(monitor())
