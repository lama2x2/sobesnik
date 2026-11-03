"""Страницы веб-кабинета (009 §6). Каждая — один-два вызова API; правил оценки и прогресса
здесь нет.

Где идёт тренировка — в адресе (`/sessions/{id}?q=3`), кто вошёл — в cookie: перезапуск веба
ничего не теряет.
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, File, Form, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from sobesnik_web import render
from sobesnik_web.api import (
    LEVELS,
    Answer,
    ApiError,
    Difficulty,
    Gate,
    Level,
    LlmStatus,
    Review,
    Session,
)
from sobesnik_web.auth import CodeThrottle, User, clear_login, set_login
from sobesnik_web.settings import WebSettings

log = logging.getLogger(__name__)

router = APIRouter()


def _templates(request: Request) -> Jinja2Templates:
    templates: Jinja2Templates = request.app.state.templates
    return templates


def _settings(request: Request) -> WebSettings:
    settings: WebSettings = request.app.state.settings
    return settings


def _gate(request: Request) -> Gate:
    gate: Gate = request.app.state.gate
    return gate


def page(
    request: Request, name: str, context: dict[str, Any], status_code: int = 200
) -> HTMLResponse:
    return _templates(request).TemplateResponse(request, name, context, status_code=status_code)


def go(request: Request, url: str) -> Response:
    """Переход после формы: HTMX просит заголовком, обычная форма — редиректом."""
    if request.headers.get("hx-request") == "true":
        return Response(status_code=200, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


def flash(request: Request, message: str) -> HTMLResponse:
    """Сообщение в блок `#flash` страницы — для HTMX-форм (ошибка ввода без вызова API)."""
    return _templates(request).TemplateResponse(
        request,
        "_flash.html",
        {"message": message},
        headers={"HX-Retarget": "#flash", "HX-Reswap": "innerHTML"},
    )


# --- вход (009 §5) ---------------------------------------------------------------


def _login_page(request: Request, error: str | None = None, status_code: int = 200) -> HTMLResponse:
    context = {"me": None, "error": error, "local_login": _settings(request).web_local_login}
    return page(request, "login.html", context, status_code)


def _logged_in(request: Request, token: str) -> Response:
    response = RedirectResponse("/", status_code=303)
    set_login(response, token, _settings(request))
    return response


@router.get("/login")
async def login_page(request: Request) -> HTMLResponse:
    return _login_page(request)


@router.post("/login/local")
async def login_local(request: Request) -> Response:
    try:
        token = await _gate(request).local_login()
    except ApiError as exc:
        return _login_page(request, render.api_error_text(exc), 400)
    return _logged_in(request, token.token)


@router.post("/login/code")
async def login_code(request: Request, code: Annotated[str, Form()] = "") -> Response:
    throttle: CodeThrottle = request.app.state.throttle
    if not throttle.allowed():
        return _login_page(request, render.CODE_SLOW_DOWN, 429)
    try:
        token = await _gate(request).redeem(code=code)
    except ApiError as exc:
        if exc.code in ("link_invalid", "validation_error"):
            throttle.record(ok=False)
            return _login_page(request, render.LINK_INVALID, 400)
        return _login_page(request, render.api_error_text(exc), 502)
    throttle.record(ok=True)
    return _logged_in(request, token.token)


@router.get("/login/{secret}")
async def login_confirm(request: Request, secret: str) -> HTMLResponse:
    """Ссылка из бота сама не входит: нужно нажать кнопку. Так её не «съест» предпросмотр
    Telegram или браузера, который открывает адрес сам (009 §5.2)."""
    return page(request, "login_confirm.html", {"me": None, "secret": secret})


@router.post("/login/{secret}")
async def login_by_link(request: Request, secret: str) -> Response:
    try:
        token = await _gate(request).redeem(secret=secret)
    except ApiError as exc:
        text = render.LINK_INVALID if exc.code == "link_invalid" else render.api_error_text(exc)
        return _login_page(request, text, 400)
    return _logged_in(request, token.token)


@router.post("/logout")
async def logout(
    request: Request, user: User, everywhere: Annotated[bool, Form()] = False
) -> Response:
    try:
        await user.api.logout(everywhere=everywhere)
    except ApiError as exc:  # токен всё равно забываем
        log.info("logout failed", extra={"code": exc.code})
    response = RedirectResponse("/login", status_code=303)
    clear_login(response)
    return response


# --- главная и вакансия ------------------------------------------------------------


@router.get("/")
async def home(request: Request, user: User) -> HTMLResponse:
    try:
        llm = await user.api.llm_status()
    except ApiError as exc:
        llm = LlmStatus(ok=False, hint=render.api_error_text(exc))
    vacancies = await user.api.vacancies(user.me.user_id)
    progress = await user.api.get_progress(user.me.user_id)
    context = {
        "me": user.me,
        "llm": llm,
        "vacancies": vacancies,
        "active": [s for s in progress.sessions if s.status == "active"],
        "history": [s for s in progress.sessions if s.status != "active"],
    }
    return page(request, "home.html", context)


@router.post("/vacancies")
async def create_vacancy(
    request: Request,
    user: User,
    text: Annotated[str, Form()] = "",
    url: Annotated[str, Form()] = "",
    file: Annotated[UploadFile | None, File()] = None,
) -> Response:
    """Вакансия текстом, ссылкой или сохранённой страницей (VAC-1). Разбор — 30–50 с."""
    html = None
    if file is not None and file.filename:
        html = (await file.read()).decode("utf-8", errors="replace") or None
    if html:
        vacancy = await user.api.create_vacancy(html=html)
    elif url.strip():
        vacancy = await user.api.create_vacancy(url=url.strip())
    elif text.strip():
        vacancy = await user.api.create_vacancy(text=text.strip())
    else:
        return flash(request, render.NO_SOURCE)
    return go(request, f"/vacancies/{vacancy.id}")


@router.get("/vacancies/{vacancy_id}")
async def vacancy_page(request: Request, user: User, vacancy_id: uuid.UUID) -> HTMLResponse:
    vacancy = await user.api.get_vacancy(vacancy_id)
    return page(request, "vacancy.html", {"me": user.me, "vacancy": vacancy, "levels": LEVELS})


@router.post("/vacancies/{vacancy_id}/level")
async def set_level(
    request: Request, user: User, vacancy_id: uuid.UUID, level: Annotated[Level, Form()]
) -> HTMLResponse:
    vacancy = await user.api.set_level(vacancy_id, level)
    return page(request, "_profile.html", {"vacancy": vacancy, "levels": LEVELS})


@router.post("/vacancies/{vacancy_id}/requirements/{requirement_id}/remove")
async def remove_requirement(
    request: Request, user: User, vacancy_id: uuid.UUID, requirement_id: uuid.UUID
) -> HTMLResponse:
    vacancy = await user.api.remove_requirement(vacancy_id, requirement_id)
    return page(request, "_profile.html", {"vacancy": vacancy, "levels": LEVELS})


@router.post("/vacancies/{vacancy_id}/train")
async def train(
    request: Request,
    user: User,
    vacancy_id: uuid.UUID,
    n: Annotated[int, Form(ge=render.QUESTIONS_MIN, le=render.QUESTIONS_MAX)] = (
        render.QUESTIONS_DEFAULT
    ),
    topics: Annotated[list[str] | None, Form()] = None,
    required_only: Annotated[bool, Form()] = False,
    difficulty: Annotated[Difficulty, Form()] = "same",
) -> Response:
    """Генерация вопросов — около 30–40 с; темы не отмечены — по всей вакансии; настройки —
    только обязательные и сложность (010 §5)."""
    session = await user.api.create_session(
        vacancy_id, n, topics or None, required_only, difficulty
    )
    return go(request, f"/sessions/{session.id}")


# --- тренировка ------------------------------------------------------------------------


def _answer_context(
    answer: Answer,
    session_id: uuid.UUID,
    position: int,
    total: int,
    review: Review | None = None,
) -> dict[str, Any]:
    elapsed = int((datetime.now(UTC) - answer.created_at).total_seconds())
    return {
        "answer": answer,
        "review": review,
        "session_id": session_id,
        "position": position,
        "total": total,
        "elapsed": max(0, elapsed),
        "slow": elapsed >= render.SLOW_AFTER_S,
    }


@router.get("/sessions/{session_id}")
async def session_page(
    request: Request,
    user: User,
    session_id: uuid.UUID,
    q: int | None = None,
    skipped: int | None = None,
) -> Response:
    session = await user.api.get_session(session_id)
    total = len(session.questions)
    if session.status == "finished" and q is None:
        return RedirectResponse(f"/sessions/{session_id}/summary", status_code=303)
    position = q if q is not None else session.first_open()
    context: dict[str, Any] = {
        "me": user.me,
        "session": session,
        "total": total,
        "position": position,
        "skipped": session.question(skipped) if skipped else None,
        "question": session.question(position),
        "answer": None,
    }
    question = context["question"]
    if question is not None and question.answer is not None:
        answer = await user.api.get_answer(question.answer.id)
        review = await user.api.get_review(answer.id) if answer.has_review else None
        context.update(_answer_context(answer, session_id, position, total, review))
    return page(request, "session.html", context)


@router.post("/sessions/{session_id}/questions/{question_id}/answer")
async def answer_text(
    request: Request,
    user: User,
    session_id: uuid.UUID,
    question_id: uuid.UUID,
    position: Annotated[int, Form()],
    total: Annotated[int, Form()],
    text: Annotated[str, Form()] = "",
) -> HTMLResponse:
    if not text.strip():
        return flash(request, render.EMPTY_ANSWER)
    accepted = await user.api.answer_text(session_id, question_id, text.strip())
    answer = await user.api.get_answer(accepted.answer_id)
    log.info("answer sent", extra={"answer_id": str(answer.id), "kind": "text"})
    return page(request, "_answer.html", _answer_context(answer, session_id, position, total))


@router.post("/sessions/{session_id}/questions/{question_id}/voice")
async def answer_voice(
    request: Request,
    user: User,
    session_id: uuid.UUID,
    question_id: uuid.UUID,
    position: Annotated[int, Form()],
    total: Annotated[int, Form()],
    file: Annotated[UploadFile, File()],
) -> HTMLResponse:
    """Запись из браузера (webm/opus, в Safari — mp4) или файл — в API как есть, на диск и в лог
    не пишется (009 §7)."""
    data = await file.read()
    accepted = await user.api.answer_voice(
        session_id,
        question_id,
        data,
        file.filename or "voice",
        file.content_type or "application/octet-stream",
    )
    answer = await user.api.get_answer(accepted.answer_id)
    log.info(
        "answer sent", extra={"answer_id": str(answer.id), "kind": "voice", "bytes": len(data)}
    )
    return page(request, "_answer.html", _answer_context(answer, session_id, position, total))


@router.get("/answers/{answer_id}/status")
async def answer_status(
    request: Request, user: User, answer_id: uuid.UUID, position: int, total: int
) -> HTMLResponse:
    """Фрагмент статуса: пока ответ в работе, браузер спрашивает его снова через 2 с (009 §8)."""
    answer = await user.api.get_answer(answer_id)
    if answer.session_id is None:
        raise ApiError(404, "answer_not_found", "ответ не найден")
    return page(
        request, "_answer.html", _answer_context(answer, answer.session_id, position, total)
    )


@router.post("/answers/{answer_id}/review")
async def review(request: Request, user: User, answer_id: uuid.UUID) -> HTMLResponse:
    """«📖 Разобрать подробно» (010 §4): фрагмент вместо кнопки; ошибка — в блок сообщений."""
    result = await user.api.review_answer(answer_id)
    log.info("review shown", extra={"answer_id": str(answer_id)})
    return page(request, "_review.html", {"review": result, "open": True})


@router.post("/sessions/{session_id}/finish")
async def finish(request: Request, user: User, session_id: uuid.UUID) -> Response:
    await user.api.finish_session(session_id)
    return go(request, f"/sessions/{session_id}/summary")


def _summary_rows(session: Session) -> list[dict[str, Any]]:
    rows = []
    for q in sorted(session.questions, key=lambda q: q.position):
        a = q.answer
        rows.append(
            {
                "position": q.position,
                "text": q.text,
                "topic": q.topic,
                "total": a.total if a else None,
                "passed": a.passed if a else None,
                "status": a.status if a else None,
            }
        )
    return rows


@router.get("/sessions/{session_id}/summary")
async def summary_page(request: Request, user: User, session_id: uuid.UUID) -> Response:
    """Сводка (006 §8.5): без среднего балла — среднее из 3–5 шумных чисел обманчиво."""
    session = await user.api.get_session(session_id)
    if session.status != "finished":
        return RedirectResponse(f"/sessions/{session_id}", status_code=303)
    summary = await user.api.finish_session(session_id)  # повтор ничего не меняет — только сводка
    rows = _summary_rows(session)
    scored = [r for r in rows if r["total"] is not None]
    weakest = min(scored, key=lambda r: r["total"]) if len(scored) > 1 else None
    context = {
        "me": user.me,
        "session": session,
        "summary": summary,
        "rows": rows,
        "weakest": weakest,
    }
    return page(request, "summary.html", context)


# --- прогресс, повторение, мои данные --------------------------------------------------


@router.get("/progress")
async def progress_page(request: Request, user: User) -> HTMLResponse:
    progress = await user.api.get_progress(user.me.user_id)
    groups = {
        status: [t for t in progress.topics if t.status == status]
        for status in ("weak", "few", "ok")
    }
    context = {
        "me": user.me,
        "progress": progress,
        "groups": groups,
    }
    return page(request, "progress.html", context)


@router.post("/repeat")
async def repeat(
    request: Request,
    user: User,
    n: Annotated[int, Form(ge=render.QUESTIONS_MIN, le=render.QUESTIONS_MAX)] = (
        render.QUESTIONS_DEFAULT
    ),
    difficulty: Annotated[Difficulty, Form()] = "same",
    force: Annotated[bool, Form()] = False,
) -> Response:
    """Новые вопросы по темам, у которых подошёл срок SM-2 (С5, PRG-3, 011 §5) — около 40 с;
    `force` — сроки не подошли, темы с ближайшим сроком."""
    session = await user.api.create_repeat(n, difficulty, force=force)
    return go(request, f"/sessions/{session.id}")


@router.get("/data")
async def data_page(request: Request, user: User) -> HTMLResponse:
    return page(request, "data.html", {"me": user.me, "result": None})


@router.post("/data/forget")
async def forget(
    request: Request, user: User, scope: Annotated[Literal["audio", "all"], Form()]
) -> HTMLResponse:
    """С6: удалить голос и расшифровки или всё (ASR-5, 007 §7.3). Подтверждение — в браузере."""
    out = await user.api.forget(user.me.user_id, scope)
    if scope == "all":
        text = render.forgotten_text(
            out.audio_deleted,
            out.transcripts_deleted,
            vacancies=out.vacancies_deleted or 0,
            answers=out.answers_deleted,
        )
    else:
        text = render.forgotten_text(out.audio_deleted, out.transcripts_deleted)
    return page(request, "_result.html", {"message": text})
