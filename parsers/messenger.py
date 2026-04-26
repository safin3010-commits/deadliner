import json
import asyncio
import hashlib
import datetime
import re
from config import COOKIES_MESSENGER_FILE, UFA_TZ
from storage import is_seen, add_seen_message

MESSENGER_URL = "https://messenger.360.yandex.ru"

# ─── Точные селекторы из реального HTML ───────────────────────────────
CHAT_ITEM_SEL     = "[class*='yamb-chat-list-item__content']"
CHAT_NAME_SEL     = ".yamb-chat-list-item__name"
CHAT_PREVIEW_SEL  = "[class*='ui-entity-block-multi-line']"
CHAT_BADGE_SEL    = ".yamb-chat-list-item__badges"

POPUP_CLOSE_SELS = [
    "[class*='onboarding__close']",
    ".ui-popup__close",
    "[class*='popup__close']",
    "[aria-label='Close']",
    "[aria-label='Закрыть']",
    "button[class*='close']",
]

MSG_SELS = [
    "[class*='yamb-message__text']",
    "[class*='yamb-message-text']",
    "[class*='yamb-bubble__text']",
    "[class*='yamb-message__body']",
]


# ─── Cookies ──────────────────────────────────────────────────────────

def _load_raw() -> dict:
    try:
        with open(COOKIES_MESSENGER_FILE, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"url": MESSENGER_URL, "cookies": []}
    if isinstance(data, dict) and "cookies" in data:
        return data
    if isinstance(data, list):
        return {"url": MESSENGER_URL, "cookies": data}
    return {"url": MESSENGER_URL, "cookies": []}


def load_cookies() -> list:
    return _load_raw()["cookies"]


def save_cookies(cookies: list) -> None:
    existing = _load_raw()
    with open(COOKIES_MESSENGER_FILE, "w") as f:
        json.dump({"url": existing.get("url", MESSENGER_URL), "cookies": cookies}, f, indent=2)


def cookies_exist() -> bool:
    return bool(load_cookies())


# ─── Утилиты ──────────────────────────────────────────────────────────

def _stable_msg_id(sender: str, text: str) -> str:
    text_norm = re.sub(r'\s+', ' ', text.strip())[:120]
    content = f"{sender.strip().lower()}:{text_norm}"
    h = hashlib.md5(content.encode("utf-8")).hexdigest()[:12]
    return f"messenger_{h}"


# ─── Закрытие попапов ─────────────────────────────────────────────────

async def _close_popups(page) -> None:
    for sel in POPUP_CLOSE_SELS:
        try:
            btns = await page.query_selector_all(sel)
            for btn in btns:
                if await btn.is_visible():
                    await btn.click()
                    await page.wait_for_timeout(400)
                    print(f"  Messenger: закрыт попап по '{sel}'")
        except Exception:
            pass
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(300)
    except Exception:
        pass


# ─── Список чатов ─────────────────────────────────────────────────────

async def _parse_chat_list(page) -> list[dict]:
    """
    Читаем список чатов. Определяем непрочитанные тремя способами:
    1. Бейдж с числом (yamb-chat-list-item__badges непустой)
    2. Жирный шрифт у текста preview (font-weight >= 600)
    3. Класс 'unread' у родительского элемента
    """
    try:
        await page.wait_for_selector(CHAT_ITEM_SEL, timeout=20_000)
    except Exception:
        print("  Messenger: список чатов не загрузился")
        return []

    # Ждём пока загрузятся бейджи — они приходят чуть позже контента
    await page.wait_for_timeout(3_000)

    # Скроллим список чатов вниз и обратно — форсируем загрузку бейджей
    try:
        list_el = await page.query_selector(CHAT_ITEM_SEL)
        if list_el:
            await list_el.evaluate("el => el.parentElement.scrollBy(0, 300)")
            await page.wait_for_timeout(500)
            await list_el.evaluate("el => el.parentElement.scrollBy(0, -300)")
            await page.wait_for_timeout(1_000)
    except Exception:
        pass

    items = await page.query_selector_all(CHAT_ITEM_SEL)
    print(f"  Messenger: чатов в списке: {len(items)}")

    chats = []
    for el in items:
        try:
            # Имя чата
            name_el = await el.query_selector(CHAT_NAME_SEL)
            name = (await name_el.inner_text()).strip() if name_el else ""
            if not name:
                name = (await el.inner_text()).strip().split("\n")[0][:60]
            if not name:
                continue

            # Preview — текст последнего сообщения
            preview = ""
            preview_el = await el.query_selector(CHAT_PREVIEW_SEL)
            if preview_el:
                preview = (await preview_el.inner_text()).strip()

            # Определяем непрочитанные тремя способами
            has_unread = False

            # Способ 1: бейдж с числом непрочитанных
            badge_el = await el.query_selector(CHAT_BADGE_SEL)
            if badge_el:
                badge_html = await badge_el.inner_html()
                if badge_html.strip():
                    has_unread = True
                    print(f"    [{name}] непрочитан (бейдж)")

            # Способ 2: жирный шрифт у preview
            if not has_unread and preview_el:
                try:
                    fw = await preview_el.evaluate(
                        "el => parseInt(window.getComputedStyle(el).fontWeight)"
                    )
                    if fw and fw >= 600:
                        has_unread = True
                        print(f"    [{name}] непрочитан (жирный шрифт, fw={fw})")
                except Exception:
                    pass

            # Способ 3: класс 'unread' у родителя
            if not has_unread:
                try:
                    parent_class = await el.evaluate(
                        "el => el.parentElement ? el.parentElement.className : ''"
                    )
                    if "unread" in str(parent_class).lower():
                        has_unread = True
                        print(f"    [{name}] непрочитан (класс родителя)")
                except Exception:
                    pass

            chats.append({
                "name": name,
                "preview": preview[:300],
                "has_unread": has_unread,
                "el": el,
            })
        except Exception:
            continue

    unread_count = sum(1 for c in chats if c["has_unread"])
    print(f"  Messenger: всего чатов: {len(chats)}, непрочитанных: {unread_count}")
    return chats


# ─── Чтение сообщений внутри чата ─────────────────────────────────────

async def _read_open_chat(page, chat_name: str) -> list[dict]:
    """Читаем последние новые сообщения из открытого чата."""
    await page.wait_for_timeout(2_000)
    messages = []

    msg_els = []
    for sel in MSG_SELS:
        found = await page.query_selector_all(sel)
        if found:
            msg_els = found[-5:]
            print(f"    сообщения по '{sel}' ({len(found)}), читаем {len(msg_els)}")
            break

    if not msg_els:
        for cont_sel in [
            "[class*='yamb-chat-content']",
            "[class*='yamb-messages']",
            "[class*='yamb-thread']",
            "main",
        ]:
            cont = await page.query_selector(cont_sel)
            if cont:
                divs = await cont.query_selector_all("div[class]")
                candidates, seen = [], set()
                for d in divs:
                    try:
                        t = (await d.inner_text()).strip()
                        if 3 < len(t) < 500 and t not in seen:
                            seen.add(t)
                            candidates.append(d)
                    except Exception:
                        continue
                if candidates:
                    msg_els = candidates[-5:]
                    break

    for el in msg_els:
        try:
            text = (await el.inner_text()).strip()
            if not text or len(text) < 2:
                continue
            msg_id = _stable_msg_id(chat_name, text)
            if is_seen(msg_id):
                continue
            messages.append({
                "id": msg_id,
                "sender": chat_name,
                "text": text[:600],
                "source": "messenger",
            })
        except Exception:
            continue

    return messages


# ─── Главный поток ────────────────────────────────────────────────────

async def _fetch_from_page(context) -> list[dict]:
    page = await context.new_page()

    print("  Messenger: загружаем страницу...")
    loaded = False
    for attempt in range(3):
        try:
            await page.goto(MESSENGER_URL, wait_until="domcontentloaded", timeout=40_000)
            loaded = True
            break
        except Exception as e:
            print(f"  Messenger: попытка {attempt+1} не удалась: {e}")
            await page.wait_for_timeout(3_000)
    if not loaded:
        print("  Messenger: страница не загрузилась после 3 попыток")
        await page.close()
        return []
    await page.wait_for_timeout(5_000)

    await _close_popups(page)
    await page.wait_for_timeout(1_000)

    now = datetime.datetime.now(tz=UFA_TZ)
    all_messages = []
    seen_ids: set[str] = set()

    chats = await _parse_chat_list(page)
    unread = [c for c in chats if c["has_unread"]]

    # Берём только preview — не заходим внутрь чата чтобы не помечать как прочитанное
    for chat in unread:
        preview = chat["preview"].strip() or "[новое сообщение]"
        if not preview:
            continue

        msg_id = _stable_msg_id(chat["name"], preview)
        if is_seen(msg_id) or msg_id in seen_ids:
            continue

        seen_ids.add(msg_id)
        # add_seen_message вызывается после успешной отправки в scheduler
        all_messages.append({
            "id": msg_id,
            "sender": chat["name"],
            "text": preview,
            "date": now.strftime("%d.%m.%Y %H:%M"),
            "source": "messenger",
        })
        print(f"  Messenger: новое от '{chat['name']}': {preview[:50]}")

    save_cookies(await context.cookies())
    await page.close()

    return all_messages


# ─── Публичный интерфейс ──────────────────────────────────────────────

async def fetch_new_messages() -> list:
    """
    Получаем непрочитанные сообщения из Яндекс Мессенджера.
    Для авторизации: python3 login_messenger.py
    """
    print("Messenger: проверяем сообщения...")

    cookies = load_cookies()
    if not cookies:
        print("Messenger: нет cookies. Запусти: python3 login_messenger.py")
        return []

    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            )
            await context.add_cookies(cookies)

            check = await context.new_page()
            loaded = False
            for attempt in range(3):
                try:
                    await check.goto(MESSENGER_URL, wait_until="domcontentloaded", timeout=40_000)
                    loaded = True
                    break
                except Exception:
                    await check.wait_for_timeout(2_000)
            if not loaded:
                print("Messenger: сессия недоступна (сеть)")
                await check.close()
                await browser.close()
                return []
            if any(x in check.url for x in ("passport", "login", "auth")):
                print("Messenger: сессия истекла. Запусти: python3 login_messenger.py")
                await check.close()
                await browser.close()
                return []
            await check.close()

            messages = await _fetch_from_page(context)
            await browser.close()

        print(f"Messenger: итого новых: {len(messages)}")
        return messages

    except Exception as e:
        print(f"Messenger fetch failed: {e}")
        import traceback
        traceback.print_exc()
        return []
