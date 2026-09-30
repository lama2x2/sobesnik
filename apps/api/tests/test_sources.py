"""Загрузка по ссылке и извлечение текста — без сети и без БД."""

from typing import Any

import httpx
import pytest

from sobesnik_api.sources.extract import extract_vacancy_text, is_hh, page_host, truncate
from sobesnik_api.sources.fetch import (
    MAX_PAGE_BYTES,
    UrlNotAllowedError,
    UrlUnreadableError,
    is_public_ip,
)

URL = "https://jobs.example.com/vacancy/7"


@pytest.mark.parametrize(
    ("ip", "public"),
    [
        ("93.184.216.34", True),
        ("2606:4700::1111", True),
        ("127.0.0.1", False),
        ("10.1.2.3", False),
        ("172.16.0.1", False),
        ("192.168.1.1", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),
        ("0.0.0.0", False),
        ("224.0.0.1", False),
        ("::1", False),
        ("fc00::1", False),
        ("fe80::1%eth0", False),
        ("::ffff:127.0.0.1", False),
    ],
)
def test_is_public_ip(ip: str, public: bool) -> None:
    assert is_public_ip(ip) is public


async def test_fetch_ok(web: Any, replies: Any) -> None:
    web.pages[URL] = replies.html("<p>ok</p>")
    page = await web.fetcher().fetch(URL)
    assert page.content == b"<p>ok</p>"
    assert page.url == URL


async def test_fetch_follows_redirects(web: Any, replies: Any) -> None:
    web.pages[URL] = httpx.Response(302, headers={"location": "/v/8"})
    web.pages["https://jobs.example.com/v/8"] = replies.html("<p>ok</p>")
    page = await web.fetcher().fetch(URL)
    assert page.url == "https://jobs.example.com/v/8"


async def test_redirect_to_private_address_rejected(web: Any) -> None:
    web.pages[URL] = httpx.Response(302, headers={"location": "http://internal.local/"})
    web.dns["internal.local"] = ["10.0.0.5"]
    with pytest.raises(UrlNotAllowedError):
        await web.fetcher().fetch(URL)
    assert len(web.requests) == 1


async def test_mixed_dns_answer_rejected(web: Any) -> None:
    web.dns["jobs.example.com"] = ["93.184.216.34", "127.0.0.1"]
    with pytest.raises(UrlNotAllowedError):
        await web.fetcher().fetch(URL)
    assert web.requests == []


async def test_allow_private(web: Any, replies: Any) -> None:
    web.dns["jobs.example.com"] = ["127.0.0.1"]
    web.pages[URL] = replies.html("<p>ok</p>")
    assert (await web.fetcher(allow_private=True).fetch(URL)).content == b"<p>ok</p>"


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "javascript:alert(1)", "https:///nohost", "http://x:99999/"]
)
async def test_bad_urls(web: Any, url: str) -> None:
    with pytest.raises(UrlNotAllowedError):
        await web.fetcher().fetch(url)


async def test_too_many_redirects(web: Any) -> None:
    for i in range(7):
        web.pages[f"https://jobs.example.com/{i}"] = httpx.Response(
            302, headers={"location": f"/{i + 1}"}
        )
    with pytest.raises(UrlUnreadableError):
        await web.fetcher().fetch("https://jobs.example.com/0")


@pytest.mark.parametrize(
    "page",
    [
        httpx.Response(403),
        httpx.Response(302),
        httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"}),
        httpx.Response(
            200, content=b"x" * (MAX_PAGE_BYTES + 1), headers={"content-type": "text/html"}
        ),
        httpx.ReadTimeout("slow"),
        httpx.ConnectError("refused"),
    ],
    ids=["403", "redirect-without-location", "pdf", "too-big", "timeout", "connect"],
)
async def test_unreadable(web: Any, page: object) -> None:
    web.pages[URL] = page
    with pytest.raises(UrlUnreadableError):
        await web.fetcher().fetch(URL)


async def test_unresolvable_host(web: Any) -> None:
    async def fail(host: str, port: int) -> list[str]:
        raise OSError("NXDOMAIN")

    fetcher = web.fetcher()
    fetcher._resolver = fail
    with pytest.raises(UrlUnreadableError):
        await fetcher.fetch(URL)


HH = """<html><head><meta property="og:url" content="https://kazan.hh.ru/vacancy/5"></head>
<body><h1 data-qa="vacancy-title">Go-разработчик</h1>
<div data-qa="vacancy-description"><p>Пишем сервисы на Go.</p><ul><li>gRPC</li><li>Kafka</li></ul>
</div></body></html>"""


def test_extract_hh_by_og_url() -> None:
    text = extract_vacancy_text(HH)
    assert text is not None
    assert text.startswith("Должность: Go-разработчик")
    assert "Описание:\nПишем сервисы на Go.\n- gRPC\n- Kafka" in text
    assert "Ключевые навыки" not in text


def test_hh_without_markup_falls_back_to_generic() -> None:
    article = "<p>" + "Мы ищем инженера по данным. " * 20 + "</p>"
    html = f"<html><body><article><h1>Инженер данных</h1>{article}</article></body></html>"
    text = extract_vacancy_text(html, "https://hh.ru/vacancy/1")
    assert text is not None
    assert "Мы ищем инженера по данным" in text


def test_extract_generic_skips_navigation() -> None:
    body = "".join(
        f"<p>Требование номер {i}: знать SQL и Python на хорошем уровне.</p>" for i in range(8)
    )
    html = (
        "<html><body><nav><a href='/'>Главная</a><a href='/jobs'>Все вакансии</a></nav>"
        f"<main><article><h1>Аналитик</h1>{body}</article></main>"
        "<footer>© Компания, все права защищены</footer></body></html>"
    )
    text = extract_vacancy_text(html, URL)
    assert text is not None
    assert "Требование номер 7" in text
    assert "все права защищены" not in text


@pytest.mark.parametrize("html", ["", "   ", "<html></html>", b"\x00\x01"])
def test_extract_nothing(html: str | bytes) -> None:
    assert not extract_vacancy_text(html, URL)


def test_page_host() -> None:
    import lxml.html

    doc = lxml.html.fromstring(
        '<html><head><link rel="canonical" href="https://HH.ru/v/1"></head></html>'
    )
    assert page_host(doc, None) == "hh.ru"
    assert page_host(doc, "https://other.org/x") == "other.org"


@pytest.mark.parametrize(
    ("host", "expected"),
    [("hh.ru", True), ("spb.hh.ru", True), ("hh.kz", True), ("ohh.ru", False), (None, False)],
)
def test_is_hh(host: str | None, expected: bool) -> None:
    assert is_hh(host) is expected


def test_truncate() -> None:
    text = "абзац один\n\n" + "а" * 50 + "\n\nхвост"
    assert truncate(text, 66) == "абзац один\n\n" + "а" * 50
    assert truncate(text, 69) == text
    assert truncate("x" * 100, 10) == "x" * 10


def test_decode_html() -> None:
    from sobesnik_api.sources.extract import decode_html

    cp1251 = '<html><head><meta charset="windows-1251"></head><p>Вакансия</p></html>'.encode(
        "cp1251"
    )
    assert "Вакансия" in decode_html(cp1251)
    assert "Вакансия" in decode_html("<p>Вакансия</p>".encode(), None)
    assert "Вакансия" in decode_html("<p>Вакансия</p>".encode("cp1251"), "windows-1251")
    assert decode_html(b"\xff\xfe<p>", "no-such-codec")


def test_extract_bytes_with_xml_declaration() -> None:
    html = '<?xml version="1.0" encoding="utf-8"?>' + HH
    text = extract_vacancy_text(html.encode(), None, "utf-8")
    assert text is not None
    assert "Go-разработчик" in text
