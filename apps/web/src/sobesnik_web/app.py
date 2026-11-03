"""Сборка веб-кабинета (009 §4). Без побочных эффектов при импорте — его используют тесты."""

import hashlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from sobesnik_web import render
from sobesnik_web.api import ApiError, ApiGate, Gate
from sobesnik_web.auth import CodeThrottle, LoginRequired, clear_login
from sobesnik_web.routes import router
from sobesnik_web.settings import WebSettings

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def asset_version() -> str:
    """Хэш статики для `?v=` в ссылках: после пересборки браузер не берёт старую статику из кэша."""
    digest = hashlib.sha256()
    for path in sorted((HERE / "static").iterdir()):
        digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


def make_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=HERE / "templates")
    env = templates.env
    env.globals.update(
        ASSET_V=asset_version(),
        band=render.band,
        approx=render.approx,
        dots=render.dots,
        criterion_title=render.criterion_title,
        attempts_word=render.attempts_word,
        topic_score=render.topic_score,
        status_line=render.status_line,
        failure_text=render.failure_text,
        LEVEL_TITLES=render.LEVEL_TITLES,
        APPROX_NOTE=render.APPROX_NOTE,
        PROGRESS_NOTE=render.PROGRESS_NOTE,
        PRIVACY_NOTE=render.PRIVACY_NOTE,
        DOUBTS_SHOWN=render.DOUBTS_SHOWN,
        SLOW_AFTER_S=render.SLOW_AFTER_S,
        QUESTIONS_MIN=render.QUESTIONS_MIN,
        QUESTIONS_MAX=render.QUESTIONS_MAX,
        QUESTIONS_DEFAULT=render.QUESTIONS_DEFAULT,
        ANSWER_MAX_CHARS=render.ANSWER_MAX_CHARS,
        VACANCY_MAX_CHARS=render.VACANCY_MAX_CHARS,
    )
    return templates


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def create_app(settings: WebSettings | None = None, gate: Gate | None = None) -> FastAPI:
    """`gate` передают тесты (фейк API); без него — клиент к `API_URL`."""
    settings = settings or WebSettings()
    templates = make_templates()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        token = settings.api_service_token
        app.state.gate = gate or ApiGate(
            settings.api_url, token.get_secret_value() if token else None
        )
        yield
        await app.state.gate.aclose()

    app = FastAPI(title="Sobesnik", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.templates = templates
    app.state.throttle = CodeThrottle()
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.include_router(router)

    @app.middleware("http")
    async def same_origin_only(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Формы принимаются только со своих страниц (009 §6): cookie `SameSite=Lax` и проверка
        `Origin` вместе закрывают подделку запроса с чужого сайта."""
        origin = request.headers.get("origin")
        foreign = bool(origin) and urlsplit(origin).netloc != request.headers.get("host")
        if request.method in UNSAFE_METHODS and foreign:
            log.warning("cross-origin form rejected", extra={"path": request.url.path})
            return Response("Запрос с чужого сайта отклонён.", status_code=403)
        return await call_next(request)

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, _: LoginRequired) -> Response:
        response: Response
        if is_htmx(request):
            response = Response(status_code=200, headers={"HX-Redirect": "/login"})
        else:
            response = RedirectResponse("/login", status_code=303)
        clear_login(response)
        return response

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError) -> Response:
        """Ошибка API — понятным текстом: в HTMX — в блок сообщений страницы, иначе — страницей."""
        log.info("api error", extra={"path": request.url.path, "code": exc.code})
        text = render.api_error_text(exc)
        if is_htmx(request):
            return templates.TemplateResponse(
                request,
                "_flash.html",
                {"message": text},
                headers={"HX-Retarget": "#flash", "HX-Reswap": "innerHTML"},
            )
        status = exc.status if exc.status in (404, 409, 422) else 502
        context: dict[str, Any] = {"message": text, "me": None}
        return templates.TemplateResponse(request, "error.html", context, status_code=status)

    @app.get("/health", include_in_schema=False)
    async def health() -> HTMLResponse:
        return HTMLResponse("ok")

    return app
