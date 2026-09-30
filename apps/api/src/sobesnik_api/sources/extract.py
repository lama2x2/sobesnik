"""HTML → текст вакансии: отдельный разборщик для hh.ru и общий для остальных сайтов."""

import copy
import re
from urllib.parse import urlsplit

import lxml.html
import trafilatura
from lxml.etree import ParserError

from sobesnik_core.limits import MAX_VACANCY_CHARS

HH_HOSTS = ("hh.ru", "hh.kz", "hh.uz")
_META_CHARSET = re.compile(rb"""<meta[^>]+charset=["']?([\w-]+)""", re.IGNORECASE)
_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>")
_BLOCK_TAGS = ("p", "div", "li", "ul", "ol", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr")


def is_hh(host: str | None) -> bool:
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in HH_HOSTS)


def decode_html(content: bytes, charset: str | None = None) -> str:
    """Кодировка из заголовка, иначе из <meta charset>, иначе UTF-8."""
    candidates = [charset]
    if match := _META_CHARSET.search(content[:4096]):
        candidates.append(match.group(1).decode("ascii"))
    for encoding in candidates:
        if encoding:
            try:
                return content.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                continue
    return content.decode("utf-8", errors="replace")


def _parse(html: str) -> lxml.html.HtmlElement | None:
    html = _XML_DECL.sub("", html, count=1)
    if not html.strip():
        return None
    try:
        return lxml.html.fromstring(html)
    except (ParserError, ValueError):
        return None


def page_host(doc: lxml.html.HtmlElement, url: str | None) -> str | None:
    """Хост из ссылки, а для сохранённой страницы — из canonical или og:url."""
    candidates = [url] if url else []
    candidates += doc.xpath("//link[@rel='canonical']/@href")
    candidates += doc.xpath("//meta[@property='og:url']/@content")
    for candidate in candidates:
        host = urlsplit(str(candidate)).hostname
        if host:
            return host.lower()
    return None


def element_text(element: lxml.html.HtmlElement) -> str:
    """Текст с переносами строк на границах блоков и маркерами у пунктов списка."""
    element = copy.deepcopy(element)  # правим хвосты у копии, а не у документа
    element.tail = None
    for node in element.iter(*_BLOCK_TAGS):
        if node.tag == "li":
            node.text = "- " + (node.text or "")
        node.tail = "\n" + (node.tail or "")
    lines = (" ".join(line.split()) for line in element.text_content().splitlines())
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _first_text(doc: lxml.html.HtmlElement, qa: str) -> str:
    nodes = doc.xpath(f"//*[@data-qa='{qa}']")
    return element_text(nodes[0]) if nodes else ""


def extract_hh(doc: lxml.html.HtmlElement) -> str | None:
    description = _first_text(doc, "vacancy-description")
    if not description:
        return None
    parts = []
    if title := _first_text(doc, "vacancy-title"):
        parts.append(f"Должность: {title}")
    if experience := _first_text(doc, "vacancy-experience"):
        parts.append(f"Требуемый опыт работы: {experience}")
    parts.append(f"Описание:\n{description}")
    skills = [element_text(n) for n in doc.xpath("//*[@data-qa='skills-element']")]
    if skills := [s for s in skills if s]:
        parts.append("Ключевые навыки: " + ", ".join(skills))
    return "\n\n".join(parts)


def extract_generic(html: str, url: str | None) -> str | None:
    text = trafilatura.extract(
        html, url=url, include_comments=False, include_tables=True, favor_recall=True
    )
    return text.strip() if text else None


def truncate(text: str, limit: int = MAX_VACANCY_CHARS) -> str:
    """Обрезка по границе абзаца, иначе строки, иначе по лимиту."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    for sep in ("\n\n", "\n"):
        cut = head.rfind(sep)
        if cut > limit // 2:
            return head[:cut].rstrip()
    return head


def extract_vacancy_text(
    html: str | bytes, url: str | None = None, charset: str | None = None
) -> str | None:
    """Текст вакансии со страницы или None, если извлечь нечего."""
    if isinstance(html, bytes):
        html = decode_html(html, charset)
    doc = _parse(html)
    if doc is None:
        return None
    text = extract_hh(doc) if is_hh(page_host(doc, url)) else None
    text = text or extract_generic(html, url)
    return truncate(text) if text else None
