import imaplib
import email
import email.header
import email.utils
import datetime
import re
import ssl
import time
from html.parser import HTMLParser
from config import YANDEX_MAIL, YANDEX_APP_PASSWORD, GMAIL_ADDRESS, GMAIL_APP_PASSWORD, UFA_TZ
from storage import is_seen

IMAP_HOST = "imap.yandex.ru"
GMAIL_IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
# Яндекс иногда рвёт TLS-соединение на середине (SSLEOFError) — примерно в
# 1 из 9 проверок по логам, чисто сетевой обрыв, не проблема аккаунта или
# кода. Раньше это тихо гасило всю проверку до следующего 15-минутного
# тика; теперь один быстрый повтор той же попытки.
MAIL_CONNECT_RETRIES = 2
MAIL_RETRY_DELAY_SEC = 3


def decode_header_value(value: str) -> str:
    decoded_parts = email.header.decode_header(value)
    result = []
    for part, encoding in decoded_parts:
        if isinstance(part, bytes):
            try:
                result.append(part.decode(encoding or "utf-8", errors="replace"))
            except (LookupError, UnicodeDecodeError):
                result.append(part.decode("utf-8", errors="replace"))
        else:
            result.append(str(part))
    return "".join(result)


class _HTMLToText(HTMLParser):
    """HTML → чистый читаемый текст без Markdown-мусора."""

    def __init__(self):
        super().__init__()
        self.result = []
        self._skip = False
        self._in_link = False
        self._link_text = []
        self._link_href = ""

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip = True
            return
        if tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4"):
            self.result.append("\n")
        if tag == "a":
            self._in_link = True
            self._link_text = []
            for name, val in attrs:
                if name == "href" and val and val.startswith("http"):
                    self._link_href = val

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self._skip = False
        if tag == "a" and self._in_link:
            text = "".join(self._link_text).strip()
            # Показываем только текст ссылки без URL — URL часто длинный и некрасивый
            if text:
                self.result.append(text)
            self._in_link = False
            self._link_text = []
            self._link_href = ""

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_link:
            self._link_text.append(data)
        else:
            self.result.append(data)

    def get_text(self) -> str:
        text = "".join(self.result)
        # Убираем пробелы/табы вокруг переносов
        text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
        # Схлопываем 2+ переноса в один
        text = re.sub(r"\n{2,}", "\n", text)
        # Убираем множественные пробелы
        text = re.sub(r"[ \t]{2,}", " ", text)
        # Убираем строки состоящие только из пробелов/спецсимволов
        lines = [l for l in text.split("\n") if l.strip()]
        return "\n".join(lines).strip()


def html_to_text(html: str) -> str:
    parser = _HTMLToText()
    try:
        parser.feed(html)
        return parser.get_text()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html).strip()


def get_email_body(msg) -> str:
    """Извлекаем текст письма, конвертируя HTML в читаемый вид."""
    text_plain = ""
    text_html = ""

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain" and not text_plain:
                try:
                    charset = part.get_content_charset() or "utf-8"
                    text_plain = part.get_payload(decode=True).decode(charset, errors="replace")
                except Exception:
                    pass
            elif content_type == "text/html" and not text_html:
                try:
                    charset = part.get_content_charset() or "utf-8"
                    text_html = part.get_payload(decode=True).decode(charset, errors="replace")
                except Exception:
                    pass
    else:
        try:
            charset = msg.get_content_charset() or "utf-8"
            raw = msg.get_payload(decode=True).decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                text_html = raw
            else:
                text_plain = raw
        except Exception:
            pass

    # Предпочитаем HTML — он обычно богаче по содержанию
    if text_html:
        body = html_to_text(text_html)
    elif text_plain:
        # Чистим plain text так же
        body = re.sub(r"[ \t]*\n[ \t]*", "\n", text_plain)
        body = re.sub(r"\n{2,}", "\n", body)
        body = re.sub(r"[ \t]{2,}", " ", body)
        lines = [l for l in body.split("\n") if l.strip()]
        body = "\n".join(lines).strip()
    else:
        body = ""

    return body.strip()


def _fetch_new_emails_sync(host: str, user: str, password: str, id_prefix: str, label: str, source: str,
                            from_filter: str = None) -> list:
    print(f"Mail ({label}): проверяем почту...")

    mail = None
    for attempt in range(1, MAIL_CONNECT_RETRIES + 1):
        try:
            mail = imaplib.IMAP4_SSL(host, IMAP_PORT, timeout=30)
            mail.login(user, password)
            mail.select("INBOX")
            break
        except (OSError, ssl.SSLError) as e:
            # Обрыв соединения/TLS — сетевая ошибка, не ошибка логина.
            # imaplib.IMAP4.error (например неверный пароль) сюда не попадает
            # и уходит в обработчик ниже без повтора.
            mail = None
            if attempt >= MAIL_CONNECT_RETRIES:
                print(f"Mail ({label}): не удалось подключиться после {attempt} попыток: {e!r}")
                return []
            print(f"Mail ({label}): обрыв соединения ({e!r}), повтор через {MAIL_RETRY_DELAY_SEC}с...")
            time.sleep(MAIL_RETRY_DELAY_SEC)

    new_emails = []
    try:
        if from_filter:
            status, message_ids = mail.uid("search", None, "FROM", from_filter, "UNSEEN")
        else:
            status, message_ids = mail.uid("search", None, "UNSEEN")
        if status != "OK":
            return []

        ids = message_ids[0].split()
        print(f"Mail ({label}): найдено непрочитанных писем: {len(ids)}")

        for msg_id in ids:
            msg_id_str = msg_id.decode()
            seen_id = f"{id_prefix}{msg_id_str}"
            if is_seen(seen_id):
                continue

            status, data = mail.uid("fetch", msg_id, "(BODY.PEEK[])")
            if status != "OK":
                continue

            msg = email.message_from_bytes(data[0][1])

            subject = decode_header_value(msg.get("Subject", "Без темы"))
            sender = decode_header_value(msg.get("From", "Неизвестный"))
            date_str = msg.get("Date", "")

            # Чистим имя отправителя
            sender_clean = sender
            m = re.match(r'^"?([^"<]+)"?\s*<[^>]+>$', sender)
            if m:
                sender_clean = m.group(1).strip().strip('"')

            try:
                date = email.utils.parsedate_to_datetime(date_str)
                date_formatted = date.astimezone(UFA_TZ).strftime("%d.%m.%Y %H:%M")
            except Exception:
                date_formatted = date_str

            body = get_email_body(msg)

            new_emails.append({
                "id": seen_id,
                "subject": subject,
                "sender": sender_clean,
                "date": date_formatted,
                "body": body,
                "source": source,
            })

            # add_seen_message вызывается после успешной отправки в scheduler
            pass

        print(f"Mail ({label}): новых писем: {len(new_emails)}")
        return new_emails

    except imaplib.IMAP4.error as e:
        print(f"Mail ({label}) IMAP error: {e!r}")
        return []
    finally:
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                try:
                    mail.shutdown()
                except Exception:
                    pass


async def fetch_new_emails() -> list:
    """Async обёртка — запускаем синхронный IMAP (Яндекс) в отдельном потоке."""
    import asyncio as _asyncio
    try:
        return await _asyncio.to_thread(
            _fetch_new_emails_sync, IMAP_HOST, YANDEX_MAIL, YANDEX_APP_PASSWORD, "mail_", "Яндекс", "mail"
        )
    except Exception as e:
        print(f"Mail fetch failed: {e!r}")
        return []


# Gmail-ящик пользователя завален посторонней почтой (десятки тысяч
# писем) — учебные приходят только от Нетологии, поэтому фильтруем
# по отправителю прямо на уровне IMAP SEARCH (дёшево), а не постфактум.
GMAIL_FROM_FILTER = "netology.ru"


async def fetch_new_gmail_emails() -> list:
    """То же самое для Gmail — тот же IMAP-механизм через app password
    (Google требует включённую 2FA для генерации app password, обычный
    пароль от аккаунта тут не сработает: myaccount.google.com/apppasswords).
    Смотрим только письма от Нетологии (GMAIL_FROM_FILTER) — остальной
    инбокс боту не нужен и не подходит для автоматического анализа."""
    import asyncio as _asyncio
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        return []
    try:
        return await _asyncio.to_thread(
            _fetch_new_emails_sync, GMAIL_IMAP_HOST, GMAIL_ADDRESS, GMAIL_APP_PASSWORD, "gmail_", "Gmail", "gmail",
            GMAIL_FROM_FILTER,
        )
    except Exception as e:
        print(f"Gmail fetch failed: {e!r}")
        return []
