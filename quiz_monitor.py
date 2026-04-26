"""
Мониторинг тестов в Safari.
Запуск: python3 quiz_monitor.py
Остановка: Ctrl+C

Открываешь тест в Safari — бот забирает cookies из Safari и парсит все вопросы.
"""
import asyncio
import re
import subprocess
from config import MY_TELEGRAM_ID, TELEGRAM_TOKEN, VK_PROXY

LMS_BASE_URL = "https://lms.utmn.ru"
PROXY = VK_PROXY or ""
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
}

_sent_attempts: set = set()


def get_safari_url() -> str | None:
    try:
        import sys
        if sys.platform == "darwin":
            # macOS — через osascript
            result = subprocess.run(
                ["osascript", "-e", 'tell application "Google Chrome" to return URL of active tab of front window'],
                capture_output=True, text=True, timeout=3
            )
            url = result.stdout.strip()
        elif sys.platform == "win32":
            # Windows — через PowerShell
            result = subprocess.run(
                ["powershell", "-command",
                 "(Get-Process chrome | Where-Object {$_.MainWindowTitle -ne ''} | Select-Object -First 1).MainWindowTitle"],
                capture_output=True, text=True, timeout=3
            )
            # На Windows получаем заголовок окна, не URL — используем pygetwindow как fallback
            # Простейший вариант: читаем из буфера обмена если пользователь скопировал URL
            url = ""
        else:
            # Linux — через xdotool
            result = subprocess.run(
                ["xdotool", "getactivewindow", "getwindowname"],
                capture_output=True, text=True, timeout=3
            )
            url = ""
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

        qt = re.search(r'class="qtext"[^>]*>(.*?)</div>\s*</div>', part, re.DOTALL)
        if not qt:
            qt = re.search(r'class="qtext"[^>]*>(.*?)</div>', part, re.DOTALL)
        if not qt:
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
        import requests as _req
        def _send():
            return _req.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": MY_TELEGRAM_ID, "text": text},
                timeout=10
            )
        await asyncio.to_thread(_send)
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"TG error: {e}")


async def fetch_page_with_safari_cookies(url: str) -> str | None:
    """Загружаем страницу используя cookies из Safari — та же сессия."""
    cookies = get_safari_cookies()
    if not cookies:
        print("⚠️ Не удалось получить cookies из Chrome")
        return None

    try:
        import requests as _requests
        def _do_get():
            s = _requests.Session()
            s.headers.update(HEADERS)
            s.cookies.update(cookies)
            if PROXY:
                s.proxies = {"https": PROXY, "http": PROXY}
            return s.get(url, timeout=20)
        r = await asyncio.to_thread(_do_get)
        if "login" in str(r.url).lower():
            return None
        return r.text
    except Exception as e:
        print(f"Fetch error: {e}")
        return None


async def fetch_next_page(attempt_id: str, cmid: str, from_page: int, sesskey: str, slots: str, cookies: dict) -> str | None:
    """POST на processattempt с cookies Safari."""
    try:
        import requests as _requests
        def _do_post():
            s = _requests.Session()
            s.headers.update(HEADERS)
            s.cookies.update(cookies)
            if PROXY:
                s.proxies = {"https": PROXY, "http": PROXY}
            return s.post(
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
                         "Referer": f"{LMS_BASE_URL}/mod/quiz/attempt.php?attempt={attempt_id}&cmid={cmid}&page={from_page}"},
                timeout=20
            )
        r = await asyncio.to_thread(_do_post)
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

    # Берём cookies прямо из Chrome — та же авторизованная сессия
    cookies = get_safari_cookies()
    if not cookies:
        await send_tg("❌ Не удалось получить сессию из Chrome. Убедись что Chrome открыт и ты залогинен в LMS.")
        return

    import requests as _requests
    _session = _requests.Session()
    _session.max_redirects = 100
    _session.headers.update(HEADERS)
    _session.cookies.update(cookies)
    if PROXY:
        _session.proxies = {"https": PROXY, "http": PROXY}

    def _get_page(url):
        fresh = get_safari_cookies()
        if fresh:
            _session.cookies.update(fresh)
        return _session.get(url, timeout=20)

    def _post_page(cmid, data, referer):
        return _session.post(
            f"{LMS_BASE_URL}/mod/quiz/processattempt.php?cmid={cmid}",
            data=data,
            headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded", "Referer": referer},
            timeout=20
        )

    # Загружаем страницу 0
    def _fetch0():
        r = _get_page(f"{LMS_BASE_URL}/mod/quiz/attempt.php?attempt={attempt_id}&cmid={cmid}&page=0")
        if "login" in str(r.url).lower():
            return None
        return r.text

    html = await asyncio.to_thread(_fetch0)
    if not html:
        await send_tg("❌ Не удалось загрузить тест. Возможно сессия истекла — обнови страницу в Chrome.")
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

    for page_num in range(total_pages):
        def _fetch_page(pn=page_num):
            r = _get_page(f"{LMS_BASE_URL}/mod/quiz/attempt.php?attempt={attempt_id}&cmid={cmid}&page={pn}")
            if "login" in str(r.url).lower():
                return None
            return r.text
        page_html = await asyncio.to_thread(_fetch_page)
        if not page_html:
            print(f"⚠️ Страница {page_num+1} не загрузилась")
            continue

        questions = _parse_questions(page_html)
        all_questions.extend(questions)
        print(f"  Страница {page_num+1}: {len(questions)} вопросов")

    if not all_questions:
        await send_tg("⚠️ Вопросы не найдены. Попробуй открыть тест заново.")
        return

    for q in all_questions:
        lines = [f"❓ Вопрос {q['num']}\n\n{q['text']}"]
        if q["answers"]:
            lines.append("\nВарианты:")
            for ans in q["answers"]:
                lines.append(f"  {ans}")
        msg = "\n".join(lines)
        print(f"Отправляю вопрос {q['num']}, длина {len(msg)}")
        await send_tg(msg)
        print(f"Отправлен вопрос {q['num']}")


    await send_tg(f"✅ Готово! Все {len(all_questions)} вопросов отправлены")
    print(f"✅ Готово — {len(all_questions)} вопросов")


async def monitor():
    print("🔍 Мониторинг Chrome запущен")
    print("   Открывай тесты в Chrome — вопросы придут в Telegram")
    print("   Ctrl+C для остановки\n")

    # Сообщение отправляется из handlers.py

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


if __name__ == "__main__":
    asyncio.run(monitor())