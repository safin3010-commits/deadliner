import os
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

# Telegram
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MY_TELEGRAM_ID = int(os.getenv("MY_TELEGRAM_ID"))

# Modeus
MODEUS_USERNAME = os.getenv("MODEUS_USERNAME")
MODEUS_PASSWORD = os.getenv("MODEUS_PASSWORD")

# LMS
LMS_USERNAME = os.getenv("LMS_USERNAME")
LMS_PASSWORD = os.getenv("LMS_PASSWORD")

# Netology
NETOLOGY_EMAIL = os.getenv("NETOLOGY_EMAIL")
NETOLOGY_PASSWORD = os.getenv("NETOLOGY_PASSWORD")

# Yandex Mail
YANDEX_MAIL = os.getenv("YANDEX_MAIL")
YANDEX_APP_PASSWORD = os.getenv("YANDEX_APP_PASSWORD")

# Timezone
UFA_TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Yekaterinburg"))

# Parsing schedule (Ufa time)
PARSE_HOURS = [9, 14, 21]

# Paths
DATA_DIR = "data"
TASKS_FILE = f"{DATA_DIR}/tasks.json"
SEEN_MESSAGES_FILE = f"{DATA_DIR}/seen_messages.json"
TOKENS_FILE = f"{DATA_DIR}/tokens.json"
COOKIES_MESSENGER_FILE = f"{DATA_DIR}/cookies_messenger.json"
COOKIES_MAIL_FILE = f"{DATA_DIR}/cookies_mail.json"

VK_USER_TOKEN = os.getenv("VK_USER_TOKEN")

# Yandex TTS
YANDEX_TTS_KEY = os.getenv("YANDEX_TTS_KEY")

# OpenRouter — читаем все ключи OPENROUTER_KEY_* динамически
import random as _random
_openrouter_keys = []
_i = 1
while True:
    _k = os.getenv(f"OPENROUTER_KEY_{_i}")
    if not _k:
        break
    _openrouter_keys.append(_k)
    _i += 1
_random.shuffle(_openrouter_keys)
OPENROUTER_KEYS = _openrouter_keys

# OpenWeatherMap
OPENWEATHER_KEY = os.getenv("OPENWEATHER_KEY")

# Groq — фоллбэк когда OpenRouter исчерпан
_groq_keys = []
_gi = 1
while True:
    _gk = os.getenv(f"GROQ_KEY_{_gi}")
    if not _gk:
        break
    _groq_keys.append(_gk)
    _gi += 1
_random.shuffle(_groq_keys)
GROQ_KEYS = _groq_keys

# Личные данные
USER_NAME = os.getenv("USER_NAME", "Студент")
USER_CITY = os.getenv("USER_CITY", "")

# Погода
_lat = os.getenv("WEATHER_LAT", "").strip()
_lon = os.getenv("WEATHER_LON", "").strip()
WEATHER_LAT = float(_lat) if _lat else 55.7558
WEATHER_LON = float(_lon) if _lon else 37.6173

# ВКонтакте
VK_CHAT_URL = os.getenv("VK_CHAT_URL", "")
VK_PROXY = os.getenv("VK_PROXY", "")

# Chrome path — платформо-зависимый дефолт
import platform as _platform
_sys_platform = _platform.system()
if not os.getenv("CHROME_PATH"):
    if _sys_platform == "Windows":
        CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    elif _sys_platform == "Linux":
        CHROME_PATH = "/usr/bin/google-chrome"
    else:
        CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
else:
    CHROME_PATH = os.getenv("CHROME_PATH")
