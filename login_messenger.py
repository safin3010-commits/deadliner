"""
Запускать ОТДЕЛЬНО от бота, один раз:
    cd ~/anti_laziness_bot && source venv/bin/activate && python3 login_messenger.py
"""

import asyncio
import json
import os

COOKIES_FILE  = "data/cookies_messenger.json"
MESSENGER_URL = "https://messenger.360.yandex.ru"


async def main():
    from playwright.async_api import async_playwright

    os.makedirs("data", exist_ok=True)

    print("=" * 50)
    print("  Авторизация Яндекс Мессенджера")
    print("=" * 50)
    print()

    async with async_playwright() as p:
        browser = None

        for channel, label in [("chrome", "системный Chrome"), (None, "встроенный Chromium")]:
            print(f"Запускаем {label}...")
            try:
                if channel:
                    browser = await p.chromium.launch(channel=channel, headless=False)
                else:
                    browser = await p.chromium.launch(
                        headless=False,
                        args=["--no-sandbox", "--disable-dev-shm-usage"],
                    )
                print(f"✅ {label} запущен")
                break
            except Exception as e:
                print(f"  Не удалось: {e}")
                browser = None

        if not browser:
            print("❌ Не удалось запустить браузер")
            return

        context = await browser.new_context(
            viewport=None,
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        print(f"Открываем {MESSENGER_URL} ...")
        await page.goto(MESSENGER_URL, timeout=30_000)

        print()
        print("Браузер открыт.")
        print("1. Войди в аккаунт Яндекс если нужно")
        print("2. Дождись загрузки списка чатов")
        print("3. Вернись сюда и нажми Enter")
        print()

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, input, ">>> Нажми Enter когда мессенджер загрузился: ")

        await page.wait_for_timeout(1500)

        cookies = await context.cookies()
        with open(COOKIES_FILE, "w") as f:
            json.dump({"url": MESSENGER_URL, "cookies": cookies}, f, indent=2)

        print()
        print(f"✅ Сохранено {len(cookies)} cookies → {COOKIES_FILE}")
        await browser.close()

    print()
    print("Готово! Теперь запускай бота: python3 main.py")


if __name__ == "__main__":
    asyncio.run(main())
