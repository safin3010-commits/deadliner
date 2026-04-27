"""
Парсер ВК через Playwright — берёт сегодняшние сообщения из беседы.
Каждое новое сообщение форматирует через DeepSeek и отправляет в бот.
"""
import json
import os
import asyncio
import datetime
import hashlib
from config import UFA_TZ, VK_CHAT_URL, VK_PROXY, CHROME_PATH

# VK_CHAT_URL берётся из config
VK_SEEN_FILE = "data/vk_seen.json"
VK_COOKIES_FILE = "data/vk_cookies.json"
# VK_PROXY берётся из config
# CHROME_PATH берётся из config


def _load_seen() -> dict:
    try:
        with open(VK_SEEN_FILE) as f:
            return json.load(f)
    except Exception:
        return {"seen_hashes": []}


def _save_seen(data: dict):
    os.makedirs("data", exist_ok=True)
    with open(VK_SEEN_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False)


def _is_hash_seen(msg_hash: str) -> bool:
    seen = _load_seen()
    return msg_hash in seen.get("seen_hashes", [])


def _mark_hash_seen(msg_hash: str):
    seen = _load_seen()
    hashes = seen.get("seen_hashes", [])
    if msg_hash not in hashes:
        hashes.append(msg_hash)
    if len(hashes) > 500:
        hashes = hashes[-500:]
    seen["seen_hashes"] = hashes
    _save_seen(seen)


def _decode_vk_links(text: str) -> str:
    """Декодируем VK redirect ссылки в прямые."""
    import re, urllib.parse
    def decode_link(m):
        url = m.group(0)
        if "vk.com/away.php" in url:
            try:
                parsed = urllib.parse.urlparse(url)
                params = urllib.parse.parse_qs(parsed.query)
                real = params.get("to", [url])[0]
                return urllib.parse.unquote(real)
            except Exception:
                return url
        return url
    return re.sub(r'https?://[^\s]+', decode_link, text)


async def _format_with_ai(text: str) -> str:
    """Форматируем сообщение из ВК через AI."""
    import re
    try:
        from grok import ask_grok
        system = (
            "Ты форматировщик расписания из ВКонтакте для Telegram HTML.\n"
            "Убери: шапку группы, приветствие, легенду дисциплин, строки Сообщество закрепило, строки только с vk.com ссылками.\n"
            "Оставь ВСЕ пары без исключения.\n\n"
            "Формат вывода строго как в примере ниже:\n"
            "<b>Расписание</b>\n"
            "<b>24 апреля, пятница:</b>\n\n"
            "📖 <b>06:30 (мск)</b>\n"
            "<b>История России</b>. Скипина И.В.\n\n"
            "💻 <b>15:40 (мск)</b>\n"
            "<b>Математический анализ</b>. Практика. Кутузов А.С.\n"
            "https://my.mts-link.ru/ссылка\n\n"
            "Правила:\n"
            "- Жирным ТОЛЬКО название предмета до первой точки, всё остальное обычный текст\n"
            "- Иконки: 📖 лекция, 💻 практика/лаб, 🏃 физкультура, 🎯 ВППА, 🔵 LXP/асинхрон, 📝 остальное\n"
            "- Ссылки my.mts-link.ru на отдельной строке\n"
            "- Пустая строка между парами\n"
            "- Никаких пояснений — только текст"
        )
        result = await ask_grok(text, system=system)
        if result:
            import re
            # Фикс 1: дата на двух строках если AI склеил
            result = re.sub(
                r'<b>Расписание\s+(\d[^<]+)</b>',
                r'<b>Расписание</b>\n<b>\1</b>',
                result
            )
            # Фикс 2: убираем жирный с преподавателей
            # Строка вида <b>Название. Преподаватель.</b> -> <b>Название</b>. Преподаватель.
            def fix_bold(m):
                inner = m.group(1)
                dot_pos = inner.find('.')
                if dot_pos > 0:
                    return f'<b>{inner[:dot_pos]}</b>{inner[dot_pos:]}'
                return m.group(0)
            result = re.sub(r'<b>([^<]{10,})</b>', fix_bold, result)
            return result
    except Exception as e:
        print(f"VK format AI error: {e}")
    # Фоллбэк
    lines = text.strip().split("\n")
    while lines:
        last = lines[-1].strip()
        if (re.match(r"^\d{1,2}:\d{2}", last) and "vk.com" in last) or re.match(r"^https?://vk\.com", last):
            lines.pop()
        else:
            break
    return "\n".join(lines).strip()


async def fetch_todays_vk_messages() -> list:
    """
    Открываем беседу, берём все сообщения из блока 'сегодня',
    возвращаем только новые (не виданные раньше).
    """
    try:
        from playwright.async_api import async_playwright

        if not os.path.exists(VK_COOKIES_FILE):
            print("VK: куки не найдены")
            return []

        with open(VK_COOKIES_FILE) as f:
            cookies = json.load(f)

        async with async_playwright() as p:
            _launch = {"headless": True, "executable_path": CHROME_PATH}
            if VK_PROXY:
                _launch["proxy"] = {"server": VK_PROXY}
            browser = await p.chromium.launch(**_launch)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            await context.add_cookies(cookies)
            page = await context.new_page()

            print(f"VK: открываем беседу...")
            await page.goto(VK_CHAT_URL, timeout=60000, wait_until="domcontentloaded")
            try:
                await page.wait_for_selector('[class*="ConvoHistory__dateStack"]', timeout=20000)
            except Exception:
                pass
            await page.wait_for_timeout(3000)

            if "login" in page.url:
                print("VK: куки протухли")
                await browser.close()
                try:
                    from config import TELEGRAM_TOKEN, MY_TELEGRAM_ID
                    import httpx as _httpx
                    async with _httpx.AsyncClient(timeout=10) as _client:
                        await _client.post(
                            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                            json={"chat_id": MY_TELEGRAM_ID, "text": "⚠️ VK: куки протухли — расписание не приходит. Запусти: python3 parsers/vk_browser.py --save-cookies"}
                        )
                except Exception as _e:
                    print(f"VK: не удалось отправить уведомление: {_e}")
                return []

            messages = await page.evaluate("""
                () => {
                    const results = [];
                    const stacks = document.querySelectorAll('.ConvoHistory__dateStack');
                    // Берём последний блок — это всегда "сегодня"
                    const todayStack = stacks[stacks.length - 1];
                    if (!todayStack) return results;
                    // Первая строка должна быть "сегодня"
                    const firstLine = (todayStack.innerText || '').split('\\n')[0].trim().toLowerCase();
                    if (!firstLine.startsWith('сегодня')) return results;

                    for (const child of todayStack.children) {
                        const cls = child.className || '';
                        // Пропускаем разделитель даты
                        if (cls.includes('StickyDateSeparator')) continue;
                        // Пропускаем системные уведомления о закреплении
                        const text = (child.innerText || '').trim();
                        if (text.includes('закрепило сообщение') || text.includes('закрепил сообщение')) continue;
                        if (text.length < 20) continue;

                        // Собираем ссылки
                        let fullText = text;
                        child.querySelectorAll('a[href]').forEach(a => {
                            const href = a.href;
                            if (href && href.startsWith('http') && !fullText.includes(href)) {
                                fullText += ' ' + href;
                            }
                        });
                        results.push(fullText);
                    }
                    return results;
                }
            """)

            await browser.close()

            if not messages:
                print("VK: сегодняшних сообщений не найдено")
                return []

            print(f"VK: найдено {len(messages)} сообщений за сегодня")

            new_messages = []
            for text in messages:
                if len(text.strip()) < 20:
                    continue
                # Отправляем только сообщения со ссылками
                if "http://" not in text and "https://" not in text:
                    continue
                today_str = datetime.datetime.now(tz=UFA_TZ).strftime("%Y-%m-%d")
                msg_hash = hashlib.md5(f"{today_str}:{text[:150].strip()}".encode()).hexdigest()[:16]
                if not _is_hash_seen(msg_hash):
                    new_messages.append({"text": text, "hash": msg_hash})

            print(f"VK: новых сообщений: {len(new_messages)}")
            return new_messages

    except Exception as e:
        print(f"VK browser error: {e}")
        return []


async def save_cookies(chrome_path=None, vk_proxy=None):
    from playwright.async_api import async_playwright
    _chrome = chrome_path or CHROME_PATH or None
    _proxy = vk_proxy or VK_PROXY or None
    async with async_playwright() as p:
        launch_args = {"headless": False}
        if _chrome:
            launch_args["executable_path"] = _chrome
        if _proxy:
            launch_args["proxy"] = {"server": _proxy}
        browser = await p.chromium.launch(**launch_args)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto("https://vk.com/login")
        print("Войди в ВК в открывшемся браузере, потом нажми Enter...")
        input()
        cookies = await context.cookies()
        os.makedirs("data", exist_ok=True)
        with open(VK_COOKIES_FILE, "w") as f:
            json.dump(cookies, f, ensure_ascii=False)
        print(f"Сохранено {len(cookies)} куки")
        await browser.close()


def mark_vk_sent(msg_id: int, msg_hash: str = ""):
    _mark_hash_seen(msg_hash if msg_hash else str(msg_id))


if __name__ == "__main__":
    import sys
    if "--save-cookies" in sys.argv:
        asyncio.run(save_cookies())
    else:
        async def test():
            msgs = await fetch_todays_vk_messages()
            if msgs:
                for m in msgs:
                    print(f"\n=== Новое сообщение (hash={m['hash']}) ===")
                    print(m['text'][:500])
            else:
                print("Нет новых сообщений")
        asyncio.run(test())
