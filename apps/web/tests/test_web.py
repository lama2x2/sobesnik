"""Веб-кабинет (009 §11): вход, страницы сценариев, статус ответа, голос, ошибки, защита форм."""

import ast
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient

from sobesnik_web.api import (
    ApiError,
    Evaluation,
    LlmStatus,
    Progress,
    SessionAnswer,
    Transcript,
)
from sobesnik_web.auth import COOKIE, CodeThrottle

HX = {"HX-Request": "true"}
WEB_SRC = Path(__file__).resolve().parents[1] / "src" / "sobesnik_web"


def evaluation(total: int = 70, passed: bool = True) -> Evaluation:
    return Evaluation.model_validate(
        {
            "total": total,
            "passed": passed,
            "scores": [
                {
                    "criterion": "correctness",
                    "title": "Корректность",
                    "score": 2,
                    "reason": "почти",
                },
                {
                    "criterion": "terminology",
                    "title": "Терминология",
                    "score": 3,
                    "reason": "точно",
                },
            ],
            "missed_points": ["про блокировки"],
            "advice": "Расскажите про компромиссы.",
            "asr_doubts": [],
        }
    )


def text_of(html: str) -> str:
    """Видимый текст страницы одной строкой: проверять фразы, а не разметку."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


# --- вход ----------------------------------------------------------------------------


async def test_without_login_goes_to_login(web: AsyncClient) -> None:
    for path in ("/", "/progress", "/data", f"/vacancies/{uuid.uuid4()}"):
        response = await web.get(path)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
    htmx = await web.post("/repeat", headers=HX)
    assert htmx.headers["HX-Redirect"] == "/login"


async def test_login_page(web: AsyncClient) -> None:
    page = text_of((await web.get("/login")).text)
    assert "/web" in page and "Войти по коду" in page
    assert "Войти без Telegram" in page


async def test_link_opens_confirmation_not_login(web: AsyncClient, fake: Any) -> None:
    """Предпросмотр ссылки (GET) не входит и не гасит её — только кнопка (009 §5.2)."""
    page = await web.get("/login/link-secret")
    assert page.status_code == 200
    assert 'action="/login/link-secret"' in page.text
    assert not web.cookies.get(COOKIE)
    assert "link-secret" in fake.links


async def test_login_by_link_is_telegram_user(browser: AsyncClient, fake: Any) -> None:
    page = await browser.get("/data")
    assert page.status_code == 200
    assert "тот же профиль, что в боте" in text_of(page.text)


async def test_link_works_once(web: AsyncClient) -> None:
    assert (await web.post("/login/link-secret")).status_code == 303
    web.cookies.clear()
    again = await web.post("/login/link-secret")
    assert again.status_code == 400
    assert "недействительны" in text_of(again.text)
    assert not web.cookies.get(COOKIE)


async def test_cookie_is_http_only(web: AsyncClient) -> None:
    response = await web.post("/login/link-secret")
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "path=/" in cookie


async def test_login_by_code_from_phone(web: AsyncClient, fake: Any) -> None:
    response = await web.post("/login/code", data={"code": "k7mq-2xda"})
    assert response.status_code == 303
    assert fake.calls[-1] == ("redeem", (None, "k7mq-2xda"))
    assert (await web.get("/")).status_code == 200


async def test_wrong_codes_slow_down(web: AsyncClient, fake: Any) -> None:
    for _ in range(5):
        response = await web.post("/login/code", data={"code": "AAAA-AAAA"})
        assert response.status_code == 400
    slowed = await web.post("/login/code", data={"code": "K7MQ-2XDA"})
    assert slowed.status_code == 429
    assert "Подождите" in text_of(slowed.text)
    assert sum(name == "redeem" for name, _ in fake.calls) == 5


def test_throttle_lets_through_after_pause() -> None:
    now = [100.0]
    throttle = CodeThrottle(free_tries=2, pause_s=5, clock=lambda: now[0])
    throttle.record(ok=False)
    throttle.record(ok=False)
    assert not throttle.allowed()
    now[0] += 5
    assert throttle.allowed()
    throttle.record(ok=True)
    assert throttle.failures == 0


async def test_local_login(web: AsyncClient, fake: Any) -> None:
    assert (await web.post("/login/local")).status_code == 303
    page = await web.get("/data")
    assert "без Telegram" in text_of(page.text)


async def test_local_login_hidden_when_off(fake: Any) -> None:
    from httpx import ASGITransport

    from sobesnik_web.app import create_app
    from sobesnik_web.settings import WebSettings

    app = create_app(WebSettings(web_local_login=False), gate=fake)
    transport = ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=transport, base_url="http://w") as client,
    ):
        page = await client.get("/login")
    assert "Войти без Telegram" not in page.text


async def test_logout(browser: AsyncClient, fake: Any) -> None:
    response = await browser.post("/logout")
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert fake.calls[-1] == ("logout", (False,))
    assert not browser.cookies.get(COOKIE)
    assert (await browser.get("/")).status_code == 303


async def test_logout_everywhere(browser: AsyncClient, fake: Any) -> None:
    other = fake._token(fake.telegram_user).token
    await browser.post("/logout", data={"everywhere": "true"})
    assert fake.calls[-1] == ("logout", (True,))
    assert other not in fake.tokens


async def test_revoked_token_goes_to_login(browser: AsyncClient, fake: Any) -> None:
    fake.tokens.clear()  # «Выйти везде» с другого входа
    response = await browser.get("/")
    assert response.status_code == 303 and response.headers["location"] == "/login"


# --- защита форм ---------------------------------------------------------------------


async def test_foreign_origin_rejected(browser: AsyncClient, fake: Any) -> None:
    response = await browser.post(
        "/data/forget", data={"scope": "all"}, headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403
    assert not any(name == "forget" for name, _ in fake.calls)
    same = await browser.post(
        "/data/forget", data={"scope": "audio"}, headers={"Origin": "http://web.test", **HX}
    )
    assert same.status_code == 200


async def test_html_is_escaped(browser: AsyncClient, fake: Any) -> None:
    fake.vacancy.title = "<script>alert(1)</script>"
    page = await browser.get(f"/vacancies/{fake.vacancy.id}")
    assert "<script>alert(1)" not in page.text
    assert "&lt;script&gt;" in page.text


# --- главная и вакансия ----------------------------------------------------------------


async def test_home(browser: AsyncClient, fake: Any) -> None:
    fake.progress = Progress.model_validate(
        {
            "topics": [],
            "sessions": [
                {
                    "id": str(fake.session.id),
                    "created_at": "2026-11-03T10:00:00Z",
                    "mode": "vacancy",
                    "status": "active",
                    "vacancy_title": "Python-разработчик",
                    "answered": 1,
                    "passed": 1,
                    "avg_total": 70,
                    "scores": [70, None],
                }
            ],
        }
    )
    page = text_of((await browser.get("/")).text)
    assert "Незаконченная тренировка" in page and "Продолжить" in page
    assert "Python-разработчик" in page and "Новая вакансия" in page
    assert "Модель сейчас недоступна" not in page


async def test_home_warns_when_llm_down(browser: AsyncClient, fake: Any) -> None:
    fake.llm = LlmStatus(ok=False, hint="Не достучался до Ollama: запустите её")
    page = text_of((await browser.get("/")).text)
    assert "Не достучался до Ollama" in page


@pytest.mark.parametrize(
    ("data", "files", "expected"),
    [
        ({"text": "  Ищем Python  "}, None, ("Ищем Python", None, None)),
        (
            {"url": "https://hh.ru/vacancy/1", "text": ""},
            None,
            (None, "https://hh.ru/vacancy/1", None),
        ),
        (
            {},
            {"file": ("v.html", b"<html>vac</html>", "text/html")},
            (None, None, "<html>vac</html>"),
        ),
    ],
)
async def test_create_vacancy(
    browser: AsyncClient,
    fake: Any,
    data: dict[str, str],
    files: dict[str, Any] | None,
    expected: tuple[str | None, ...],
) -> None:
    if files is None:  # форма всегда multipart: пустое поле файла тоже приходит
        files = {"file": ("", b"", "application/octet-stream")}
    response = await browser.post("/vacancies", data=data, files=files, headers=HX)
    assert response.headers["HX-Redirect"] == f"/vacancies/{fake.vacancy.id}"
    assert fake.calls[-1] == ("create_vacancy", expected)


async def test_create_vacancy_empty_form(browser: AsyncClient, fake: Any) -> None:
    response = await browser.post("/vacancies", data={"text": " "}, headers=HX)
    assert response.headers["HX-Retarget"] == "#flash"
    assert "Вставьте текст вакансии" in response.text
    assert not any(name == "create_vacancy" for name, _ in fake.calls)


async def test_create_vacancy_api_error_shown(browser: AsyncClient, fake: Any) -> None:
    fake.fail["create_vacancy"] = ApiError(422, "not_a_vacancy", "это не похоже на вакансию")
    response = await browser.post("/vacancies", data={"text": "привет"}, headers=HX)
    assert response.status_code == 200
    assert response.headers["HX-Retarget"] == "#flash"
    assert "Это не похоже на вакансию" in response.text


async def test_llm_hint_wins(browser: AsyncClient, fake: Any) -> None:
    fake.fail["create_vacancy"] = ApiError(
        503, "llm_model_missing", "нет модели", hint="Скачайте модель: ollama pull qwen3.5:4b"
    )
    response = await browser.post("/vacancies", data={"text": "вакансия"}, headers=HX)
    assert "ollama pull qwen3.5:4b" in response.text


async def test_vacancy_page_and_level(browser: AsyncClient, fake: Any) -> None:
    page = text_of((await browser.get(f"/vacancies/{fake.vacancy.id}")).text)
    assert "Python-разработчик" in page and "Asyncio" in page
    assert "Python: asyncio" in page and "БД: индексы" in page  # темы для выбора
    response = await browser.post(
        f"/vacancies/{fake.vacancy.id}/level", data={"level": "senior"}, headers=HX
    )
    assert fake.vacancy.level == "senior"
    assert 'aria-pressed="true">Senior' in response.text


async def test_remove_requirement(browser: AsyncClient, fake: Any) -> None:
    rid = fake.vacancy.requirements[0].id
    response = await browser.post(
        f"/vacancies/{fake.vacancy.id}/requirements/{rid}/remove", headers=HX
    )
    assert "Asyncio" not in response.text and "Индексы" in response.text


async def test_train_with_topics(browser: AsyncClient, fake: Any) -> None:
    response = await browser.post(
        f"/vacancies/{fake.vacancy.id}/train",
        data={"n": "3", "topics": ["db.indexes", "python.asyncio"]},
        headers=HX,
    )
    assert response.headers["HX-Redirect"] == f"/sessions/{fake.session.id}"
    assert fake.calls[-1] == (
        "create_session",
        (fake.vacancy.id, 3, ["db.indexes", "python.asyncio"]),
    )


async def test_train_whole_vacancy(browser: AsyncClient, fake: Any) -> None:
    await browser.post(f"/vacancies/{fake.vacancy.id}/train", data={"n": "5"}, headers=HX)
    assert fake.calls[-1] == ("create_session", (fake.vacancy.id, 5, None))


async def test_train_count_bounds(browser: AsyncClient, fake: Any) -> None:
    response = await browser.post(f"/vacancies/{fake.vacancy.id}/train", data={"n": "40"})
    assert response.status_code == 422
    assert not any(name == "create_session" for name, _ in fake.calls)


# --- тренировка ------------------------------------------------------------------------


async def test_session_shows_first_open_question(browser: AsyncClient, fake: Any) -> None:
    page = text_of((await browser.get(f"/sessions/{fake.session.id}")).text)
    assert "Вопрос 1 из 2" in page
    assert "Отправить ответ" in page and "Записать" in page
    assert "Пропустить вопрос" in page


async def test_skip_shows_docs_and_next(browser: AsyncClient, fake: Any) -> None:
    page = text_of((await browser.get(f"/sessions/{fake.session.id}?q=2&skipped=1")).text)
    assert "Вопрос 1 пропущен" in page and "PostgreSQL: индексы" in page
    assert "Вопрос 2 из 2" in page


async def test_text_answer_status_then_result(browser: AsyncClient, fake: Any) -> None:
    session = fake.session
    question = session.questions[0]
    response = await browser.post(
        f"/sessions/{session.id}/questions/{question.id}/answer",
        data={"text": " Индекс ускоряет поиск. ", "position": "1", "total": "2"},
        headers=HX,
    )
    assert fake.calls[-2] == ("answer_text", (question.id, "Индекс ускоряет поиск."))
    page = text_of(response.text)
    assert "Принято" in page and "Оцениваю ответ" in page
    answer_id = question.answer.id
    poll = f'hx-get="/answers/{answer_id}/status?position=1&total=2"'
    assert poll in response.text and 'hx-trigger="load delay:2s"' in response.text

    answer = fake.answers[answer_id]
    answer.status, answer.evaluation = "done", evaluation()
    done = await browser.get(f"/answers/{answer_id}/status?position=1&total=2", headers=HX)
    page = text_of(done.text)
    assert "🟡 Средне · ≈ 70 из 100" in page
    assert "про блокировки" in page and "Расскажите про компромиссы" in page
    assert "Термины" in page and "●●○" in page
    assert "hx-get" not in done.text  # опрос закончился
    assert f"/sessions/{session.id}?q=2" in done.text


async def test_empty_text_answer(browser: AsyncClient, fake: Any) -> None:
    q = fake.session.questions[0]
    response = await browser.post(
        f"/sessions/{fake.session.id}/questions/{q.id}/answer",
        data={"text": "  ", "position": "1", "total": "2"},
        headers=HX,
    )
    assert response.headers["HX-Retarget"] == "#flash"
    assert not any(name == "answer_text" for name, _ in fake.calls)


async def test_voice_goes_to_api_unchanged(browser: AsyncClient, fake: Any) -> None:
    q = fake.session.questions[0]
    audio = b"\x1aE\xdf\xa3webm-bytes"
    response = await browser.post(
        f"/sessions/{fake.session.id}/questions/{q.id}/voice",
        data={"position": "1", "total": "2"},
        files={"file": ("voice.webm", audio, "audio/webm;codecs=opus")},
        headers=HX,
    )
    assert fake.voice == [(audio, "voice.webm", "audio/webm;codecs=opus")]
    assert "Распознаю речь" in text_of(response.text)


async def test_voice_transcript_while_evaluating(browser: AsyncClient, fake: Any) -> None:
    q = fake.session.questions[0]
    await browser.post(
        f"/sessions/{fake.session.id}/questions/{q.id}/voice",
        data={"position": "1", "total": "2"},
        files={"file": ("voice.webm", b"x", "audio/webm")},
    )
    answer = fake.answers[q.answer.id]
    answer.status, answer.transcript = "evaluating", Transcript(text="индекс это дерево")
    page = text_of((await browser.get(f"/answers/{answer.id}/status?position=1&total=2")).text)
    assert "Оцениваю ответ" in page and "Вот что я услышал" in page
    assert "индекс это дерево" in page


async def test_slow_answer_note(browser: AsyncClient, fake: Any) -> None:
    q = fake.session.questions[0]
    await browser.post(
        f"/sessions/{fake.session.id}/questions/{q.id}/answer",
        data={"text": "ответ", "position": "1", "total": "2"},
    )
    answer = fake.answers[q.answer.id]
    answer.created_at = datetime.now(UTC) - timedelta(seconds=75)
    response = await browser.get(f"/answers/{answer.id}/status?position=1&total=2")
    page = text_of(response.text)
    assert "дольше обычного" in page and "ответ сохранён" in page
    assert "hx-get" in response.text  # опрос продолжается


async def test_failed_answer_offers_retry(browser: AsyncClient, fake: Any) -> None:
    q = fake.session.questions[0]
    await browser.post(
        f"/sessions/{fake.session.id}/questions/{q.id}/voice",
        data={"position": "1", "total": "2"},
        files={"file": ("voice.webm", b"x", "audio/webm")},
    )
    answer = fake.answers[q.answer.id]
    answer.status, answer.error = "failed", "asr_empty"
    response = await browser.get(f"/answers/{answer.id}/status?position=1&total=2")
    page = text_of(response.text)
    assert "Не расслышал речь" in page
    assert "hx-get" not in response.text
    assert "Отправить ответ" in page and f"/questions/{q.id}/voice" in response.text


async def test_reload_keeps_status(browser: AsyncClient, fake: Any) -> None:
    """Закрыли вкладку и вернулись — статус ответа на месте и опрос продолжается (009 §8)."""
    q = fake.session.questions[0]
    await browser.post(
        f"/sessions/{fake.session.id}/questions/{q.id}/answer",
        data={"text": "ответ", "position": "1", "total": "2"},
    )
    page = await browser.get(f"/sessions/{fake.session.id}?q=1")
    assert "Оцениваю ответ" in text_of(page.text)
    assert f"/answers/{q.answer.id}/status" in page.text
    assert "Пропустить вопрос" not in text_of(page.text)


async def test_already_answered_message(browser: AsyncClient, fake: Any) -> None:
    fake.fail["answer_text"] = ApiError(409, "already_answered", "уже есть")
    q = fake.session.questions[0]
    response = await browser.post(
        f"/sessions/{fake.session.id}/questions/{q.id}/answer",
        data={"text": "ответ", "position": "1", "total": "2"},
        headers=HX,
    )
    assert "уже есть ответ" in response.text


async def test_end_finish_and_summary(browser: AsyncClient, fake: Any) -> None:
    s = fake.session
    s.questions[0].answer = SessionAnswer(
        id=uuid.uuid4(), kind="text", status="done", total=40, passed=False
    )
    s.questions[1].answer = SessionAnswer(
        id=uuid.uuid4(), kind="text", status="done", total=80, passed=True
    )
    end = text_of((await browser.get(f"/sessions/{s.id}?q=3")).text)
    assert "Вопросы закончились" in end
    response = await browser.post(f"/sessions/{s.id}/finish", headers=HX)
    assert response.headers["HX-Redirect"] == f"/sessions/{s.id}/summary"
    page = text_of((await browser.get(f"/sessions/{s.id}/summary")).text)
    assert "Отвечено 2 из 2 · зачтено 1" in page
    assert "🔴 Слабо · ≈ 40" in page and "🟢 Хорошо · ≈ 80" in page
    assert "Слабее всего: Вопрос 1" in page
    # завершённая сессия без номера вопроса — сразу сводка
    again = await browser.get(f"/sessions/{s.id}")
    assert again.headers["location"] == f"/sessions/{s.id}/summary"


async def test_summary_of_active_session_goes_back(browser: AsyncClient, fake: Any) -> None:
    response = await browser.get(f"/sessions/{fake.session.id}/summary")
    assert response.headers["location"] == f"/sessions/{fake.session.id}"


async def test_foreign_session_not_found_page(browser: AsyncClient, fake: Any) -> None:
    fake.fail["get_session"] = ApiError(404, "session_not_found", "сессия не найдена")
    response = await browser.get(f"/sessions/{uuid.uuid4()}")
    assert response.status_code == 404
    assert "Не получилось" in text_of(response.text)


async def test_api_down_page(browser: AsyncClient, fake: Any) -> None:
    from sobesnik_web.api import ApiUnavailable

    fake.fail["get_progress"] = ApiUnavailable("ConnectError")
    response = await browser.get("/progress")
    assert response.status_code == 502
    assert "docker compose ps" in text_of(response.text)


# --- прогресс, повторение, мои данные -------------------------------------------------


def topic(
    slug: str, status: str, avg: float | None, attempts: int, low: bool = False
) -> dict[str, Any]:
    return {
        "slug": slug,
        "title": slug.upper(),
        "status": status,
        "low": low,
        "attempts": attempts,
        "last_score": 50,
        "avg_last3": avg,
        "docs": [{"title": f"Док {slug}", "url": "https://example.org"}],
    }


async def test_progress_page(browser: AsyncClient, fake: Any) -> None:
    fake.progress = Progress.model_validate(
        {
            "topics": [
                topic("db.indexes", "weak", 42.3, 3),
                topic("python.gil", "few", 45, 1, low=True),
                topic("http.rest", "ok", 81, 2),
            ],
            "sessions": [],
        }
    )
    page = text_of((await browser.get("/progress")).text)
    assert "Слабые темы" in page and "DB.INDEXES — ≈ 40 · 3 попытки" in page
    assert "Док db.indexes" in page
    assert "похоже, стоит повторить" in page
    assert "HTTP.REST — ≈ 80 · 2 попытки" in page
    assert "Повторить слабые темы" in page


async def test_progress_empty(browser: AsyncClient) -> None:
    page = text_of((await browser.get("/progress")).text)
    assert "Пока нечего показать" in page
    assert "Повторить слабые темы" not in page


async def test_repeat(browser: AsyncClient, fake: Any) -> None:
    response = await browser.post("/repeat", data={"n": "4"}, headers=HX)
    assert response.headers["HX-Redirect"] == f"/sessions/{fake.session.id}"
    assert fake.calls[-1] == ("create_repeat", (4,))


async def test_nothing_to_repeat(browser: AsyncClient, fake: Any) -> None:
    fake.fail["create_repeat"] = ApiError(409, "nothing_to_repeat", "нечего")
    response = await browser.post("/repeat", headers=HX)
    assert "Слабых тем нет" in response.text


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        ("audio", "Удалено записей: 2, расшифровок: 2."),
        ("all", "Удалено: вакансий 1, ответов 5, голосовых 2."),
    ],
)
async def test_forget(browser: AsyncClient, fake: Any, scope: str, expected: str) -> None:
    response = await browser.post("/data/forget", data={"scope": scope}, headers=HX)
    assert expected in response.text
    assert fake.calls[-1][0] == "forget" and fake.calls[-1][1][1] == scope


async def test_data_page_has_confirmations(browser: AsyncClient) -> None:
    page = await browser.get("/data")
    assert page.text.count("hx-confirm") == 2
    assert "Выйти везде" in page.text


# --- офлайн и границы ------------------------------------------------------------------


async def test_pages_load_nothing_from_internet(browser: AsyncClient, fake: Any) -> None:
    """Скрипты и стили — только свои: страница работает без интернета (009 §4)."""
    for path in ("/", "/progress", f"/sessions/{fake.session.id}", f"/vacancies/{fake.vacancy.id}"):
        html = (await browser.get(path)).text
        for src in re.findall(r'<(?:script|link)[^>]+(?:src|href)="([^"]+)"', html):
            assert src.startswith("/static/"), (path, src)
    for name in ("htmx.min.js", "app.js", "style.css"):
        assert (await browser.get(f"/static/{name}")).status_code == 200


def test_web_does_not_import_business_logic() -> None:
    """Веб — тонкий клиент: ни API, ни предметной области, ни БД и очередей (WEB-1, 009 §11)."""
    forbidden = {"sobesnik_api", "sobesnik_core", "sobesnik_bot", "sqlalchemy", "redis", "arq"}
    for path in WEB_SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert name.split(".")[0] not in forbidden, f"{path.name}: import {name}"
