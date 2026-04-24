#!/usr/bin/env python3
"""
Установка ДедЛайнер.
Запуск: python3 setup.py
"""
import os
import sys
import platform
import subprocess
import getpass

IS_WINDOWS = platform.system() == "Windows"
VENV_PYTHON = os.path.join("venv", "Scripts", "python.exe") if IS_WINDOWS else os.path.join("venv", "bin", "python3")


def clear():
    os.system("cls" if IS_WINDOWS else "clear")


def header():
    print("╔══════════════════════════════════════════╗")
    print("║     ДедЛайнер — Установка        ║")
    print("╚══════════════════════════════════════════╝")
    print()


def ask(prompt, required=True, secret=False, default=""):
    while True:
        if secret:
            val = getpass.getpass(f"{prompt}: ").strip()
        else:
            suffix = f" [{default}]" if default else " (Enter — пропустить)" if not required else ""
            val = input(f"{prompt}{suffix}: ").strip()
        if val:
            return val
        if default:
            return default
        if not required:
            return ""
        print("  ⚠️  Это поле обязательно. Попробуй ещё раз.")


def find_city_coords(city: str, openweather_key: str) -> tuple[float, float] | None:
    try:
        import urllib.request, json
        url = f"http://api.openweathermap.org/geo/1.0/direct?q={city}&limit=1&appid={openweather_key}"
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read())
            if data:
                return round(data[0]["lat"], 4), round(data[0]["lon"], 4)
    except Exception:
        pass
    return None


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
    openrouter = ask("[4/4] Ключ OpenRouter (openrouter.ai → Keys → Create)\n      Модель Gemini бесплатная, лимита нет", required=False)
    print()

    # ── Учёба ──
    print("━" * 44)
    print("УЧЁБА (можно пропустить, заполнить позже в .env)")
    print("━" * 44)
    modeus_login = ask("Логин от personal.utmn.ru", required=False)
    modeus_pass = ask("Пароль от personal.utmn.ru", required=False, secret=True) if modeus_login else ""
    lms_login = ask("Логин от lms.utmn.ru", required=False)
    lms_pass = ask("Пароль от lms.utmn.ru", required=False, secret=True) if lms_login else ""
    netology_email = ask("Email от netology.ru", required=False)
    netology_pass = ask("Пароль от netology.ru", required=False, secret=True) if netology_email else ""
    print()

    # ── Погода ──
    print("━" * 44)
    print("ПОГОДА (опционально)")
    print("━" * 44)
    openweather = ask("Ключ OpenWeatherMap (openweathermap.org)", required=False)
    city = ask("Твой город (для координат погоды)", required=False) if openweather else ""
    lat, lon = "", ""
    if city and openweather:
        coords = find_city_coords(city, openweather)
        if coords:
            lat, lon = coords
            print(f"  ✅ Координаты найдены: {lat}, {lon}")
        else:
            print("  ⚠️  Не удалось найти координаты — введи вручную в .env")
    print()

    # ── Почта ──
    print("━" * 44)
    print("ЯНДЕКС ПОЧТА (опционально)")
    print("━" * 44)
    yandex_mail = ask("Яндекс почта (@yandex.ru)", required=False)
    yandex_pass = ask("Пароль приложения (Яндекс ID → Безопасность)", required=False, secret=True) if yandex_mail else ""
    print()

    # ── Часовой пояс ──
    print("━" * 44)
    timezone = ask("Часовой пояс", required=False, default="Asia/Yekaterinburg")
    print()

    # ── Пишем .env ──
    env_lines = [
        "# Сгенерировано setup.py\n",
        f"TELEGRAM_TOKEN={tg_token}",
        f"MY_TELEGRAM_ID={tg_id}",
        f"USER_NAME={name}",
        f"TIMEZONE={timezone}",
        "",
        f"OPENROUTER_KEY_1={openrouter}",
        "",
        f"MODEUS_USERNAME={modeus_login}",
        f"MODEUS_PASSWORD={modeus_pass}",
        "",
        f"LMS_USERNAME={lms_login}",
        f"LMS_PASSWORD={lms_pass}",
        "",
        f"NETOLOGY_EMAIL={netology_email}",
        f"NETOLOGY_PASSWORD={netology_pass}",
        "",
        f"OPENWEATHER_KEY={openweather}",
        f"WEATHER_LAT={lat}",
        f"WEATHER_LON={lon}",
        f"USER_CITY={city}",
        "",
        f"YANDEX_MAIL={yandex_mail}",
        f"YANDEX_APP_PASSWORD={yandex_pass}",
        "",
        "VK_CHAT_URL=",
        "VK_PROXY=",
        "CHROME_PATH=",
    ]

    with open(".env", "w", encoding="utf-8") as f:
        f.write("\n".join(env_lines))
    print("  ✅ Файл .env создан")

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
