import imaplib
import email
import email.header
import email.utils
import datetime
import re
from html.parser import HTMLParser
from config import YANDEX_MAIL, YANDEX_APP_PASSWORD, UFA_TZ
from storage import is_seen, add_seen_message

IMAP_HOST = "imap.yandex.ru"
IMAP_PORT = 993


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


def _escape_html(text: str) -> str:
    """Экранируем спецсимволы для Telegram HTML parse_mode."""
    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


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

    # Обрезаем до разумного размера
    if len(body) > 600:
        body = body[:600] + "\n…"

    return body.strip()


def _fetch_new_emails_sync() -> list:
    print("Mail: проверяем почту...")
    new_emails = []

    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(YANDEX_MAIL, YANDEX_APP_PASSWORD)
        mail.select("INBOX")

        status, message_ids = mail.uid("search", None, "UNSEEN")
        if status != "OK":
            mail.logout()
            return []

        ids = message_ids[0].split()
        print(f"Mail: найдено непрочитанных писем: {len(ids)}")

        for msg_id in ids:
            msg_id_str = msg_id.decode()
            if is_seen(f"mail_{msg_id_str}"):
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
                "id": f"mail_{msg_id_str}",
                "subject": subject,
                "sender": sender_clean,
                "date": date_formatted,
                "body": body,
                "source": "mail",
            })

            # add_seen_message вызывается после успешной отправки в scheduler
            pass

        mail.logout()
        print(f"Mail: новых писем: {len(new_emails)}")
        return new_emails

    except imaplib.IMAP4.error as e:
        print(f"Mail IMAP error: {e}")
        return []


async def fetch_new_emails() -> list:
    """Async обёртка — запускаем синхронный IMAP в отдельном потоке."""
    import asyncio as _asyncio
    return await _asyncio.to_thread(_fetch_new_emails_sync)
    except Exception as e:
        print(f"Mail fetch failed: {e}")
        return []
