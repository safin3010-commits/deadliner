#!/usr/bin/env python3
"""
Установка ДедЛайнер.
Запуск: python3 setup.py
"""
import os
import sys
import platform
import subprocess
import asyncio

IS_WINDOWS = platform.system() == "Windows"
VENV_PYTHON = os.path.join("venv", "Scripts", "python.exe") if IS_WINDOWS else os.path.join("venv", "bin", "python3")


def clear():
    os.system("cls" if IS_WINDOWS else "clear")


def header():
    print("╔══════════════════════════════════════════╗")
    print("║        ДедЛайнер — Установка             ║")
    print("╚══════════════════════════════════════════╝")
    print()


def ask(prompt, required=True, default=""):
    while True:
        suffix = f" [{default}]" if default else " (Enter — пропустить)" if not required else ""
        val = input(f"{prompt}{suffix}: ").strip()
        if val:
            return val
        if default:
            return default
        if not required:
            return ""
        print("  ⚠️  Это поле обязательно. Попробуй ещё раз.")


def find_city_info(city: str):
    try:
        import urllib.request, urllib.parse, json
        query = urllib.parse.urlencode({"q": city, "format": "json", "limit": "1"})
        url = f"https://nominatim.openstreetmap.org/search?{query}"
        req = urllib.request.Request(url, headers={"User-Agent": "DeadlinerBot/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            if data:
                lat = round(float(data[0]["lat"]), 4)
                lon = round(float(data[0]["lon"]), 4)
                display = data[0].get("display_name", city)
                return lat, lon, display
    except Exception as e:
        print(f"  (ошибка поиска города: {e})")
    return None, None, None


def find_timezone(lat: float, lon: float) -> str:
    try:
        from timezonefinder import TimezoneFinder
        tf = TimezoneFinder()
        tz = tf.timezone_at(lat=lat, lng=lon)
        return tz or "UTC"
    except ImportError:
        pass
    try:
        import urllib.request, json
        url = f"https://timeapi.io/api/timezone/coordinate?latitude={lat}&longitude={lon}"
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
            return data.get("timeZone", "UTC")
    except Exception:
        return "UTC"


async def check_modeus(username: str, password: str) -> bool:
    try:
        import httpx
        from secrets import token_hex
        from urllib.parse import urlparse
        from bs4 import BeautifulSoup

        MODEUS_BASE_URL = "https://utmn.modeus.org"
        MODEUS_CONFIG_URL = f"{MODEUS_BASE_URL}/schedule-calendar/assets/app.config.json"
        HEADERS = {"User-Agent": "Mozilla/5.0"}

        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=HEADERS) as s:
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

            html = BeautifulSoup(r.text, "html.parser")
            form = html.find("form")
            if not form:
                return False

            form_action = form.get("action", str(r.url))
            if form_action.startswith("/"):
                p = urlparse(str(r.url))
                form_action = f"{p.scheme}://{p.netloc}{form_action}"

            login_data = {"UserName": username, "Password": password, "AuthMethod": "FormsAuthentication"}
            for inp in form.find_all("input", type="hidden"):
                login_data[inp.get("name", "")] = inp.get("value", "")

            r = await s.post(form_action, data=login_data)

            fail_signs = ["errortext", "неверный", "incorrect", "invalid", "failed"]
            for sign in fail_signs:
                if sign in r.text.lower():
                    return False
            return True
    except Exception as e:
        print(f"  (ошибка проверки: {e})")
        return False


async def check_lms(username: str, password: str) -> bool:
    try:
        import httpx, re
        LMS_BASE_URL = "https://lms.utmn.ru"
        HEADERS = {"User-Agent": "Mozilla/5.0"}

        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=HEADERS) as client:
            r = await client.get(f"{LMS_BASE_URL}/login/index.php")
            lt = re.search(r'name="logintoken"\s+value="([^"]+)"', r.text)
            login_token = lt.group(1) if lt else ""

            r = await client.post(f"{LMS_BASE_URL}/login/index.php", data={
                "username": username,
                "password": password,
                "logintoken": login_token,
                "anchor": "",
            })
            return "loginerrormessage" not in r.text and "Invalid login" not in r.text
    except Exception as e:
        print(f"  (ошибка проверки: {e})")
        return False


async def check_netology(email: str, password: str) -> bool:
    try:
        import httpx
        async with httpx.AsyncClient(base_url="https://netology.ru", timeout=20, follow_redirects=True) as s:
            r = await s.post("/backend/api/user/sign_in", json={
                "login": email,
                "password": password,
                "remember": True,
            })
            return r.status_code != 401
    except Exception as e:
        print(f"  (ошибка проверки: {e})")
        return False


def ask_with_check(prompt_login, prompt_pass, check_fn, service_name):
    while True:
        login = ask(prompt_login, required=False)
        if not login:
            return "", ""
        password = ask(prompt_pass, required=False)
        if not password:
            return login, ""

        print(f"  ⏳ Проверяю {service_name}...")
        ok = asyncio.run(check_fn(login, password))
        if ok:
            print(f"  ✅ {service_name} — авторизация успешна")
            return login, password
        else:
            print(f"  ❌ {service_name} — неверный логин или пароль")
            retry = input("  Попробовать снова? (y/n): ").strip().lower()
            if retry != "y":
                return login, password


def create_venv():
    if os.path.exists(VENV_PYTHON):
        print("  ✅ Виртуальное окружение уже есть")
        return True
    print("  ⏳ Создаю виртуальное окружение...")
    result = subprocess.run([sys.executable, "-m", "venv", "venv"], capture_output=True)
    if result.returncode != 0:
        print("  ❌ Не удалось создать venv")
        return False
    print("  ✅ Виртуальное окружение создано")
    return True


def install_deps():
    print("  ⏳ Устанавливаю зависимости (может занять пару минут)...")
    pip = os.path.join("venv", "Scripts", "pip.exe") if IS_WINDOWS else os.path.join("venv", "bin", "pip")
    result = subprocess.run([pip, "install", "-r", "requirements.txt", "-q"], capture_output=True)
    if result.returncode != 0:
        print("  ❌ Ошибка установки зависимостей")
        print(result.stderr.decode())
        return False
    print("  ✅ Зависимости установлены")
    return True


def install_playwright():
    print("  ⏳ Устанавливаю Playwright (браузер)...")
    result = subprocess.run([VENV_PYTHON, "-m", "playwright", "install", "chromium"], capture_output=True)
    if result.returncode != 0:
        print("  ⚠️  Playwright не установился — quiz и мессенджер могут не работать")
    else:
        print("  ✅ Playwright установлен")


def ask_city():
    while True:
        city = ask("Твой город (например: Уфа, Москва, Тюмень)")
        print("  ⏳ Ищу координаты города...")
        lat, lon, display = find_city_info(city)
        if lat is None:
            print("  ❌ Город не найден. Попробуй написать по-английски или проверь название.")
            retry = input("  Попробовать снова? (y/n): ").strip().lower()
            if retry != "y":
                return city, "", "", "Asia/Yekaterinburg"
            continue

        print(f"  ✅ Найдено: {display}")
        print(f"  📍 Координаты: {lat}, {lon}")
        print("  ⏳ Определяю часовой пояс...")
        timezone = find_timezone(lat, lon)
        print(f"  🕐 Часовой пояс: {timezone}")
        return city, str(lat), str(lon), timezone


def main():
    clear()
    header()
    print("Привет! Отвечай на вопросы. Enter — пропустить необязательное.")
    print()

    # ── Обязательные ──
    print("━" * 44)
    print("ОБЯЗАТЕЛЬНЫЕ НАСТРОЙКИ")
    print("━" * 44)
    name = ask("[1/4] Твоё имя (как бот будет к тебе обращаться)")
    tg_token = ask("[2/4] Telegram токен (от @BotFather)")
    tg_id = ask("[3/4] Твой Telegram ID (от @userinfobot)")
    print("[4/4] Город:")
    city, lat, lon, timezone = ask_city()
    print()

    # ── AI ──
    print("━" * 44)
    print("AI (нужен хотя бы один ключ)")
    print("━" * 44)
    print("  OpenRouter (openrouter.ai) — авто-выбор лучшей модели")
    print("  Groq (console.groq.com) — llama-3.3-70b, быстро и бесплатно")
    openrouter = ask("Ключ OpenRouter", required=False)
    groq_key = ask("Ключ Groq", required=False)
    print()

    # ── Учёба ──
    print("━" * 44)
    print("УЧЁБА (можно пропустить, заполнить позже в .env)")
    print("━" * 44)

    print("— Modeus + LMS (один логин для обоих) —")
    modeus_login, modeus_pass = ask_with_check(
        "Логин от utmn.ru",
        "Пароль от utmn.ru",
        check_modeus, "Modeus"
    )
    lms_login, lms_pass = modeus_login, modeus_pass
    if lms_login:
        print("  ⏳ Проверяю LMS...")
        ok = asyncio.run(check_lms(lms_login, lms_pass))
        print("  ✅ LMS — авторизация успешна" if ok else "  ⚠️  LMS — не удалось войти, проверь позже")

    print()
    print("— Нетология —")
    netology_email, netology_pass = ask_with_check(
        "Email от netology.ru",
        "Пароль от netology.ru",
        check_netology, "Нетология"
    )
    print()

    # ── Почта ──
    print("━" * 44)
    print("ЯНДЕКС ПОЧТА (опционально)")
    print("━" * 44)
    yandex_mail = ask("Яндекс почта (@yandex.ru)", required=False)
    yandex_pass = ask("Пароль приложения (Яндекс ID → Безопасность)", required=False) if yandex_mail else ""
    print()

    # ── Яндекс Мессенджер ──
    print("━" * 44)
    print("ЯНДЕКС МЕССЕНДЖЕР (опционально)")
    print("━" * 44)
    messenger = ask("Настроить мессенджер? (y/n)", required=False, default="n")
    if messenger.lower() == "y":
        print("  Сейчас откроется браузер — войди в Яндекс Мессенджер и нажми Enter.")
        try:
            import asyncio as _asyncio
            from parsers.messenger import MESSENGER_URL
            from playwright.async_api import async_playwright
            async def _login_messenger():
                import json, os
                async with async_playwright() as p:
                    browser = await p.chromium.launch(headless=False, args=["--no-sandbox"])
                    context = await browser.new_context()
                    page = await context.new_page()
                    await page.goto(MESSENGER_URL, timeout=30000)
                    input("  >>> Нажми Enter когда мессенджер загрузился: ")
                    cookies = await context.cookies()
                    os.makedirs("data", exist_ok=True)
                    with open("data/cookies_messenger.json", "w") as f:
                        json.dump({"url": MESSENGER_URL, "cookies": cookies}, f, indent=2)
                    print(f"  ✅ Сохранено {len(cookies)} cookies")
                    await browser.close()
            _asyncio.run(_login_messenger())
        except Exception as e:
            print(f"  ⚠️  Не удалось: {e}")
    print()

    # ── ВКонтакте ──
    print("━" * 44)
    print("ВКОНТАКТЕ (опционально — мониторинг беседы)")
    print("━" * 44)
    vk_chat_url = ask("Ссылка на беседу (например https://vk.com/im/convo/2000000022)", required=False)
    vk_proxy = ""
    chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if vk_chat_url:
        vk_proxy = ask("Прокси (например http://127.0.0.1:10808), Enter — пропустить", required=False)
    print()

    # ── Пишем .env ──
    env_lines = ["# Сгенерировано setup.py", ""]

    def add(key, val):
        if val:
            env_lines.append(f"{key}={val}")

    add("TELEGRAM_TOKEN", tg_token)
    add("MY_TELEGRAM_ID", tg_id)
    add("USER_NAME", name)
    add("TIMEZONE", timezone)
    env_lines.append("")
    add("OPENROUTER_KEY_1", openrouter)
    add("GROQ_KEY_1", groq_key)
    env_lines.append("")
    add("MODEUS_USERNAME", modeus_login)
    add("MODEUS_PASSWORD", modeus_pass)
    env_lines.append("")
    add("LMS_USERNAME", lms_login)
    add("LMS_PASSWORD", lms_pass)
    env_lines.append("")
    add("NETOLOGY_EMAIL", netology_email)
    add("NETOLOGY_PASSWORD", netology_pass)
    env_lines.append("")
    add("WEATHER_LAT", lat)
    add("WEATHER_LON", lon)
    add("USER_CITY", city)
    env_lines.append("")
    add("YANDEX_MAIL", yandex_mail)
    add("YANDEX_APP_PASSWORD", yandex_pass)
    env_lines.append("")
    add("VK_CHAT_URL", vk_chat_url)
    add("VK_PROXY", vk_proxy)
    add("CHROME_PATH", chrome_path)

    with open(".env", "w", encoding="utf-8") as f:
        f.write("\n".join(env_lines))
    print("  ✅ Файл .env создан")

    # ── ВК cookies после записи .env ──
    if vk_chat_url:
        print()
        print("  Сейчас откроется браузер — войди в ВКонтакте и нажми Enter.")
        try:
            from parsers.vk_browser import save_cookies
            asyncio.run(save_cookies(chrome_path=chrome_path, vk_proxy=vk_proxy))
            print("  ✅ ВКонтакте — cookies сохранены")
        except Exception as e:
            print(f"  ⚠️  Не удалось сохранить cookies ВК: {e}")

    # ── Установка ──
    print()
    print("━" * 44)
    print("УСТАНОВКА ЗАВИСИМОСТЕЙ")
    print("━" * 44)
    if not create_venv():
        sys.exit(1)
    if not install_deps():
        sys.exit(1)
    install_playwright()

    # ── Запуск ──
    print()
    print("━" * 44)
    go = input("Запустить бота сейчас? (y/n): ").strip().lower()
    if go == "y":
        if IS_WINDOWS:
            subprocess.Popen(["start.bat"], shell=True)
        else:
            subprocess.Popen(["bash", "start.sh"])
        print("  ✅ Бот запускается! Проверь Telegram через 10 секунд.")
    else:
        print()
        if IS_WINDOWS:
            print("  Запусти бота командой: start.bat")
        else:
            print("  Запусти бота командой: bash start.sh")

    print()
    print("Готово! 🎉")


if __name__ == "__main__":
    main()
