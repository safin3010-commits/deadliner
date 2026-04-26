import os, sys

# ── Один экземпляр ──
import tempfile as _tempfile
_PID_FILE = _tempfile.gettempdir() + "/deadliner.pid"
if os.path.exists(_PID_FILE):
    try:
        _old_pid = int(open(_PID_FILE).read().strip())
        os.kill(_old_pid, 0)  # Проверяем жив ли процесс
        print(f"❌ Бот уже запущен (PID {_old_pid}). Выходим.")
        sys.exit(0)
    except (ProcessLookupError, ValueError):
        pass  # Процесс мёртв — продолжаем
open(_PID_FILE, "w").write(str(os.getpid()))
import atexit
atexit.register(lambda: os.remove(_PID_FILE) if os.path.exists(_PID_FILE) else None)

import os, sys

import asyncio
import logging
from telegram.ext import Application
from config import TELEGRAM_TOKEN, MY_TELEGRAM_ID, MODEUS_USERNAME, LMS_USERNAME, NETOLOGY_EMAIL, YANDEX_MAIL, GROQ_KEYS, VK_CHAT_URL, USER_NAME
from bot.handlers import register_handlers
from scheduler import setup_scheduler
from storage import ensure_data_dir

# Настраиваем логирование
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)



SETUP_WARNED_FILE = "data/setup_warned.json"

def _is_setup_warned() -> bool:
    import json, os
    try:
        with open(SETUP_WARNED_FILE) as f:
            return json.load(f).get("warned", False)
    except Exception:
        return False

def _mark_setup_warned():
    import json, os
    os.makedirs("data", exist_ok=True)
    with open(SETUP_WARNED_FILE, "w") as f:
        json.dump({"warned": True}, f)

async def check_setup(bot):
    """Проверяем заполненность .env и отправляем предупреждение если что-то не заполнено."""
    if _is_setup_warned():
        return
    issues = []
    if not GROQ_KEYS:
        issues.append("⚠️ *AI* — не работает. Нужен ключ Groq (console.groq.com)")
    if not MODEUS_USERNAME:
        issues.append("⚠️ *Modeus* — нет расписания и оценок (нет MODEUS_USERNAME/PASSWORD)")
    if not LMS_USERNAME:
        issues.append("⚠️ *LMS* — нет заданий (нет LMS_USERNAME/PASSWORD)")
    if not NETOLOGY_EMAIL:
        issues.append("⚠️ *Нетология* — нет заданий (нет NETOLOGY_EMAIL/PASSWORD)")
    if not YANDEX_MAIL:
        issues.append("⚠️ *Почта* — нет уведомлений о письмах (нет YANDEX_MAIL/APP_PASSWORD)")
    if not VK_CHAT_URL:
        issues.append("⚠️ *ВКонтакте* — мониторинг беседы выключен (нет VK_CHAT_URL)")
    if not issues:
        return
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    text = "🔧 *Не заполнены некоторые настройки .env:*\n\n" + "\n\n".join(issues)
    text += "\n\n_Заполни .env и перезапусти бота._"
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Понятно, больше не показывать", callback_data="setup_warned")
    ]])
    await bot.send_message(chat_id=MY_TELEGRAM_ID, text=text, reply_markup=keyboard)

async def on_startup(app: Application):
    """Выполняется при старте бота."""
    print("=" * 50)
    print("ДедЛайнер запускается...")
    print("=" * 50)

    # Создаём папку data если нет
    ensure_data_dir()

    # Проверяем настройки .env
    await check_setup(app.bot)

    # Настраиваем планировщик
    scheduler = setup_scheduler(app.bot, MY_TELEGRAM_ID)
    scheduler.start()

    # Сохраняем scheduler в bot_data чтобы потом остановить
    app.bot_data["scheduler"] = scheduler

    # Отправляем сообщение что бот запустился
    try:
        await app.bot.send_message(
            chat_id=MY_TELEGRAM_ID,
            text=(
                "```\n"
                "🖥 ИНИЦИАЛИЗАЦИЯ СИСТЕМЫ...\n\n"
                "[██████████] 100%\n\n"
                f"> ЖЕРТВА ИДЕНТИФИЦИРОВАНА: {USER_NAME}\n"
                "> СТАТУС: подозрительно бездельничает\n"
                "> ДЕДЛАЙНЫ: найдены\n"
                "> СОВЕСТЬ: не обнаружена\n\n"
                "⚠ СЛЕЖКА АКТИВИРОВАНА\n"
                "```"
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Не удалось отправить стартовое сообщение: {e}")

    print("Бот запущен! Нажми Ctrl+C чтобы остановить.")


async def on_shutdown(app: Application):
    """Выполняется при остановке бота."""
    scheduler = app.bot_data.get("scheduler")
    if scheduler:
        scheduler.shutdown()
    print("Бот остановлен")


def main():
    """Главная функция — создаём и запускаем бота."""

    # Создаём приложение
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    # Регистрируем все хендлеры команд
    register_handlers(app)

    # Запускаем бота
    print("Запускаем бота...")
    app.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
