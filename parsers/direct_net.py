"""
Принудительный обход VPN для университетских доменов (*.utmn.ru, modeus.org).

Обнаружено 2026-09-22: при включённом VPN на машине (любом — проверено на
двух разных приложениях) системный DNS для этих доменов подменяется на
Fake-IP из диапазона 240.0.0.0/4 (зарезервирован, не маршрутизируется вне
самого VPN), а WAF на стороне университета сам по себе режет запросы,
пришедшие через VPN-узел. Из-за этого lms.utmn.ru становится недоступен
именно для бота, хотя у пользователя без VPN всё работает стабильно.

Обход — двухступенчатый (оба шага обязательны, проверено вручную):
1. Не полагаемся на системный DNS вообще — используем заранее известный
   реальный IP (KNOWN_IPS), а не то, что вернёт резолвер.
2. Привязываем исходящее соединение к локальному IP физического интерфейса
   (обычно en0, Wi-Fi/Ethernet) — это уводит трафик мимо TUN-интерфейса
   VPN на уровне сокета.

Если что-то из этого не сработает (интерфейс не найден, IP не подходит) —
тихо деградируем до обычного httpx-поведения (тот же результат, что и без
этого модуля), а не роняем вызывающий код.
"""
import contextlib
import socket
import httpx


# Реальные IP — university WAF/сервисы, вручную проверенные 2026-09-22.
# Если университет сменит инфраструктуру и это снова начнёт таймаутить —
# скорее всего, дело именно в устаревшем IP здесь, обновить вручную
# (dig +short <host> с машины БЕЗ VPN, либо спросить хостера/провайдера).
KNOWN_IPS = {
    "lms.utmn.ru": "5.1.53.144",
    "fs.utmn.ru": "5.1.53.144",
    "auth.modeus.org": "78.155.198.68",
    "utmn.modeus.org": "78.155.198.68",
}

LAN_INTERFACE = "en0"


def _lan_local_address() -> str | None:
    """IP локального физического интерфейса (не VPN-туннеля). None, если
    интерфейс не найден/недоступен — тогда просто не форсируем привязку."""
    try:
        import subprocess
        out = subprocess.run(["ifconfig", LAN_INTERFACE], capture_output=True, text=True, timeout=3).stdout
        import re
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
        return m.group(1) if m else None
    except Exception:
        return None


class _DirectIPTransport(httpx.AsyncHTTPTransport):
    """Подменяет хост в адресе на заранее известный реальный IP (см. KNOWN_IPS)
    для запросов к доменам из этого списка — DNS для них не используется
    вообще, поэтому подмена системного резолвера на Fake-IP (см. модуль)
    на это не влияет. Host/SNI остаются исходными, так что сервер и
    TLS-сертификат проверяются как обычно."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        real_ip = KNOWN_IPS.get(host)
        if not real_ip:
            return await super().handle_async_request(request)

        original_url = request.url
        request.url = request.url.copy_with(host=real_ip)
        request.headers.setdefault("Host", host)
        request.extensions["sni_hostname"] = host
        response = await super().handle_async_request(request)
        # Возвращаем request.url обратно на исходный домен ДО того, как Client
        # обработает Set-Cookie из ответа — иначе куки сессии (например,
        # MoodleSession) сохраняются под доменом IP, а не lms.utmn.ru, и
        # следующий запрос по имени хоста их не находит ("сессия завершена"
        # на каждый второй запрос, хотя логин только что прошёл успешно).
        request.url = original_url
        return response


def direct_async_client(http2: bool = False, **kwargs) -> httpx.AsyncClient:
    """httpx.AsyncClient, который для доменов из KNOWN_IPS обходит и системный
    DNS, и VPN-туннель (если он есть) — иначе ведёт себя как обычный
    httpx.AsyncClient. Использовать вместо httpx.AsyncClient(...) для
    LMS/Modeus запросов.

    http2 передаётся сюда, а не в httpx.AsyncClient(...) напрямую — раз транспорт
    уже свой (transport=...), httpx молча игнорирует http2 на уровне Client,
    его нужно передать в сам транспорт."""
    local_address = _lan_local_address()
    transport_kwargs = {"http2": http2}
    if local_address:
        transport_kwargs["local_address"] = local_address
    transport = _DirectIPTransport(**transport_kwargs)
    return httpx.AsyncClient(transport=transport, **kwargs)


@contextlib.contextmanager
def direct_dns_override():
    """Контекст-менеджер для requests/aiohttp/urllib3-кода (в отличие от
    direct_async_client — тот только для httpx): временно подменяет
    socket.getaddrinfo так, чтобы для доменов из KNOWN_IPS возвращать
    заранее известный реальный IP вместо результата системного резолвера
    (см. модульный docstring — тот в этой сети подменяет их на Fake-IP).
    Все остальные домены резолвятся как обычно — подмена не глобальная по
    содержанию, только по списку доменов.

    Используется в parsers/modeus.py::_try_auth_sync (requests, следует
    редиректам через fs.utmn.ru/auth.modeus.org — фиксированного URL нет,
    поэтому нужна подмена на уровне резолвера, а не одного адреса).

    Затрагивает весь процесс на время выполнения (socket.getaddrinfo — модульная
    функция, не потокo-локальная) — безопасно, потому что подмена не меняет
    результат для доменов вне KNOWN_IPS, а _try_auth_sync выполняется недолго
    (секунды) в отдельном потоке (asyncio.to_thread)."""
    original = socket.getaddrinfo

    def patched(host, *args, **kwargs):
        real_ip = KNOWN_IPS.get(host)
        if real_ip:
            host = real_ip
        return original(host, *args, **kwargs)

    socket.getaddrinfo = patched
    try:
        yield
    finally:
        socket.getaddrinfo = original


def requests_source_address() -> tuple[str, int] | None:
    """(ip, 0) физического интерфейса, для _SourceAddressAdapter ниже."""
    ip = _lan_local_address()
    return (ip, 0) if ip else None


def make_requests_adapter():
    """HTTPAdapter, привязывающий соединения requests.Session к локальному
    физическому интерфейсу — HTTPAdapter не принимает source_address напрямую
    в конструкторе, нужен отдельный класс, переопределяющий init_poolmanager.
    Использовать вместе с direct_dns_override() (для DNS) — сам по себе адаптер
    только уводит трафик мимо VPN-туннеля на уровне сокета, DNS не трогает."""
    import requests.adapters

    source_address = requests_source_address()
    if not source_address:
        return None

    class _SourceAddressAdapter(requests.adapters.HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            kwargs["source_address"] = source_address
            return super().init_poolmanager(*args, **kwargs)

    return _SourceAddressAdapter()
