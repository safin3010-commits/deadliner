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
UFA_TZ = ZoneInfo("Asia/Yekaterinburg")

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

# OpenRouter
OPENROUTER_KEYS = [k for k in [
    os.getenv("OPENROUTER_KEY_1"),
    os.getenv("OPENROUTER_KEY_2"),
    os.getenv("OPENROUTER_KEY_3"),
    os.getenv("OPENROUTER_KEY_4"),
] if k]

# OpenWeatherMap
OPENWEATHER_KEY = os.getenv("OPENWEATHER_KEY")
