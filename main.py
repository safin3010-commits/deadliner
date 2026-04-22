import os, sys

# ── Один экземпляр ──
_PID_FILE = "/tmp/anti_laziness_bot.pid"
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
from config import TELEGRAM_TOKEN, MY_TELEGRAM_ID
from bot.handlers import register_handlers
from scheduler import setup_scheduler
from storage import ensure_data_dir

# Настраиваем логирование
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


async def on_startup(app: Application):
    """Выполняется при старте бота."""
    print("=" * 50)
    print("Anti-Laziness Bot запускается...")
    print("=" * 50)

    # Создаём папку data если нет
    ensure_data_dir()

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
                "✅ *Anti-Laziness Bot запущен!*\n\n"
                "Я буду следить за твоими дедлайнами.\n"
                "Напиши /help чтобы узнать команды."
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
