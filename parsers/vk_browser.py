"""
Парсер ВК через Playwright — берёт сегодняшние сообщения из беседы.
Каждое новое сообщение форматирует через DeepSeek и отправляет в бот.
"""
import json
import os
import asyncio
import datetime
import hashlib
from config import UFA_TZ, VK_CHAT_URL, VK_CHAT_URLS, VK_PROXY, CHROME_PATH

# VK_CHAT_URL берётся из config
VK_SEEN_FILE = "data/vk_seen.json"
VK_COOKIES_FILE = "data/vk_cookies.json"
# VK_PROXY берётся из config
# CHROME_PATH берётся из config


_EXPIRED_ALERT_FILE = "data/vk_expired_alert.json"


def _expired_alert_due() -> bool:
    """Предупреждение о протухшем входе — не чаще раза в сутки (проверка идёт
    каждые 15 минут по каждой беседе — иначе 100+ одинаковых сообщений в день)."""
    import datetime
    now = datetime.datetime.now().timestamp()
    try:
        with open(_EXPIRED_ALERT_FILE) as f:
            last = json.load(f).get("at", 0)
    except Exception:
        last = 0
    if now - last < 24 * 3600:
        return False
    try:
        with open(_EXPIRED_ALERT_FILE, "w") as f:
            json.dump({"at": now}, f)
    except Exception:
        pass
    return True


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
    """
    1. Декодируем vk.com/away.php редиректы в прямые ссылки
    2. Убираем дублирующиеся ссылки в конце (vk.com/club..., повторные mts-link и т.д.)
    """
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

    # Сначала декодируем редиректы
    text = re.sub(r'https?://[^\s]+', decode_link, text)

    # Собираем все ссылки которые уже упомянуты в тексте (не в конце)
    all_links = re.findall(r'https?://[^\s]+', text)

    # Убираем ссылки-мусор в конце:
    # vk.com/club... — ссылка на саму беседу, не нужна
    # Повторные ссылки которые уже есть выше в тексте
    seen = set()
    def filter_link(m):
        url = m.group(0)
        # Убираем ссылки на vk.com/club (беседа)
        if re.search(r'vk\.com/club\d+', url):
            return ''
        # Убираем vk.com/away.php которые не задекодировались
        if 'vk.com/away.php' in url:
            return ''
        # Убираем дубли
        if url in seen:
            return ''
        seen.add(url)
        return url

    text = re.sub(r'https?://[^\s]+', filter_link, text)
    # Убираем повисшие пустые строки после удалённых ссылок
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]+\n', '\n', text)
    return text.strip()


async def _format_with_ai(text: str) -> str:
    """Форматируем сообщение из ВК через AI."""
    import re
    try:
        from grok import ask_grok
        system = (
            "Ты форматировщик текста для Telegram. "
            "Твоя единственная задача — сделать текст красивым для чтения в Telegram HTML, "
            "НЕ изменяя содержимое ни на одну букву.\n\n"
            "СТРОГИЕ ПРАВИЛА:\n"
            "1. Не меняй, не убирай, не добавляй ни одного слова из оригинала\n"
            "2. Не перефразируй и не переставляй предложения\n"
            "3. Весь оригинальный текст должен присутствовать дословно\n"
            "4. Используй только Telegram HTML теги: <b>жирный</b>, <i>курсив</i>\n\n"
            "ЧТО ДЕЛАТЬ:\n"
            "- Сделай жирным заголовки, даты, названия, важные слова\n"
            "- Добавь пустые строки между смысловыми блоками для читаемости\n"
            "- Убери лишние пробелы и двойные переносы строк\n"
            "- Верни текст готовым для отправки в Telegram\n\n"
            "Никаких пояснений — только отформатированный текст."
        )
        text = _decode_vk_links(text)
        result = await ask_grok(text, system=system)
        if result:
            return result
    except Exception as e:
        print(f"VK format AI error: {e!r}")
    # Фоллбэк — хотя бы декодируем ссылки
    return _decode_vk_links(text).strip()


async def fetch_todays_vk_messages(chat_url: str = None, chat_label: str = "") -> list:
    """
    Открываем беседу (chat_url, по умолчанию первая из VK_CHAT_URLS), берём все
    сообщения из блока 'сегодня', возвращаем только новые (не виданные раньше).
    chat_label используется для различения бесед в хеше и в выводе.
    """
    # VK_CHAT_URL может содержать несколько ссылок через запятую — при вызове
    # без chat_url (например ручной `python3 parsers/vk_browser.py`) берём
    # первую из VK_CHAT_URLS, а не сырую строку с запятыми внутри.
    chat_url = chat_url or (VK_CHAT_URLS[0] if VK_CHAT_URLS else "")
    if not chat_url:
        print("VK: не задан VK_CHAT_URL")
        return []
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

            print(f"VK: открываем беседу {chat_label or chat_url}...")
            await page.goto(chat_url, timeout=60000, wait_until="domcontentloaded")
            try:
                await page.wait_for_selector('[class*="ConvoHistory__dateStack"]', timeout=20000)
            except Exception:
                pass
            await page.wait_for_timeout(3000)

            try:
                page_title = await page.title()
            except Exception:
                page_title = ""
            # Протухшая сессия: ВК отдаёт страницу приглашения войти. Раньше
            # проверялся только английский заголовок «VK | Welcome!», а русский
            # «ВКонтакте | Добро пожаловать» пропускался — парсер три недели
            # молча писал «сегодняшних сообщений не найдено» (найдено
            # 2026-10-02). Плюс страховка: на странице нет ни истории беседы,
            # ни сообщений — тоже считаем, что вход слетел.
            history_count = await page.evaluate(
                "() => document.querySelectorAll('[class*=\"ConvoHistory\"], [class*=\"ConvoMessage\"]').length"
            )
            welcome = any(w in page_title for w in ("Welcome", "Добро пожаловать"))
            if "login" in page.url or welcome or history_count == 0:
                print(f"VK: куки протухли (заголовок «{page_title}», элементов беседы: {history_count})")
                await browser.close()
                if not _expired_alert_due():
                    return []
                try:
                    from config import TELEGRAM_TOKEN, MY_TELEGRAM_ID
                    import httpx as _httpx
                    async with _httpx.AsyncClient(timeout=10) as _client:
                        await _client.post(
                            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                            json={"chat_id": MY_TELEGRAM_ID, "text": "⚠️ VK: куки протухли — расписание не приходит. Запусти: venv/bin/python3 -m parsers.vk_browser --save-cookies"}
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

            today_str = datetime.datetime.now(tz=UFA_TZ).strftime("%Y-%m-%d")
            new_messages = []
            for text in messages:
                if len(text.strip()) < 20:
                    continue
                msg_hash = hashlib.md5(f"{chat_url}:{today_str}:{text[:150].strip()}".encode()).hexdigest()[:16]
                if not _is_hash_seen(msg_hash):
                    new_messages.append({"text": text, "hash": msg_hash, "chat_label": chat_label})

            print(f"VK: новых сообщений: {len(new_messages)}")
            return new_messages

    except Exception as e:
        print(f"VK browser error: {e!r}")
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
