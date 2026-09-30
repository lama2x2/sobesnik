"""Загрузка страницы вакансии по ссылке пользователя с защитой от SSRF.

Хост каждой ссылки (и каждого редиректа) резолвится, и если хотя бы один адрес не публичный,
ссылка отклоняется. Между проверкой и соединением DNS может ответить иначе (DNS rebinding);
для self-hosted инстанса этот остаточный риск принят.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

MAX_URL_CHARS = 2_000
MAX_PAGE_BYTES = 3 * 1024 * 1024
MAX_REDIRECTS = 5
USER_AGENT = "Mozilla/5.0 (compatible; Sobesnik/0.1; +https://github.com/lama2x2/sobesnik)"
HTML_TYPES = ("text/html", "application/xhtml+xml")

Resolver = Callable[[str, int], Awaitable[list[str]]]


class SourceError(Exception):
    """Причина пишется в лог, пользователю показывается общее сообщение."""


class UrlNotAllowedError(SourceError):
    pass


class UrlUnreadableError(SourceError):
    pass


@dataclass(frozen=True)
class FetchedPage:
    url: str
    """Адрес после редиректов."""
    content: bytes
    charset: str | None = None
    """Кодировка из заголовка Content-Type, если есть."""


async def system_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def is_public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def validate_url(url: str) -> tuple[str, int]:
    """Схема и хост; возвращает (host, port)."""
    if len(url) > MAX_URL_CHARS:
        raise UrlNotAllowedError("ссылка слишком длинная")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UrlNotAllowedError(f"схема {parts.scheme!r} не поддерживается")
    if not parts.hostname:
        raise UrlNotAllowedError("в ссылке нет хоста")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise UrlNotAllowedError("неверный порт") from exc
    return parts.hostname, port


async def check_host(host: str, port: int, resolver: Resolver) -> None:
    try:
        addresses = await resolver(host, port)
    except OSError as exc:
        raise UrlUnreadableError(f"хост не резолвится: {exc}") from exc
    if not addresses:
        raise UrlUnreadableError("хост не резолвится")
    if not all(is_public_ip(a) for a in addresses):
        raise UrlNotAllowedError("ссылка ведёт на внутренний адрес")


class PageFetcher:
    def __init__(
        self,
        *,
        timeout_s: float = 10.0,
        allow_private: bool = False,
        client: httpx.AsyncClient | None = None,
        resolver: Resolver = system_resolver,
    ) -> None:
        self._timeout_s = timeout_s
        self._allow_private = allow_private
        self._client = client or httpx.AsyncClient()
        self._resolver = resolver

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(self, url: str) -> FetchedPage:
        try:
            async with asyncio.timeout(self._timeout_s):
                return await self._fetch(url)
        except TimeoutError as exc:
            raise UrlUnreadableError("таймаут загрузки страницы") from exc
        except httpx.HTTPError as exc:
            raise UrlUnreadableError(f"ошибка загрузки: {type(exc).__name__}") from exc

    async def _fetch(self, url: str) -> FetchedPage:
        for _ in range(MAX_REDIRECTS + 1):
            host, port = validate_url(url)
            if not self._allow_private:
                await check_host(host, port, self._resolver)
            request = self._client.build_request(
                "GET",
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
                timeout=self._timeout_s,
            )
            response = await self._client.send(request, stream=True)
            try:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise UrlUnreadableError("редирект без адреса")
                    url = urljoin(url, location)
                    continue
                if response.status_code != 200:
                    raise UrlUnreadableError(f"страница ответила {response.status_code}")
                content_type = response.headers.get("content-type", "").lower()
                if not content_type.startswith(HTML_TYPES):
                    raise UrlUnreadableError(f"не HTML: {content_type or 'без типа'}")
                return FetchedPage(
                    url=url,
                    content=await _read_limited(response),
                    charset=response.charset_encoding,
                )
            finally:
                await response.aclose()
        raise UrlUnreadableError("слишком много редиректов")


async def _read_limited(response: httpx.Response) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_PAGE_BYTES:
            raise UrlUnreadableError("страница больше 3 МБ")
        chunks.append(chunk)
    return b"".join(chunks)
