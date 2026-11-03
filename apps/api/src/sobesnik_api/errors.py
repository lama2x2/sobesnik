"""Ошибки приложения и их HTTP-представление: {"error": {"code", "message"}}."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from sobesnik_llm import LLMError, LLMModelMissingError, LLMOutputError, LLMUnavailableError


class AppError(Exception):
    status_code = 400
    code = "error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(AppError):
    status_code = 404

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ConflictError(AppError):
    status_code = 409

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class UnprocessableError(AppError):
    """422 с кодом приложения — в отличие от стандартного 422 валидации FastAPI."""

    status_code = 422

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PayloadTooLargeError(AppError):
    status_code = 413

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ServiceUnavailableError(AppError):
    status_code = 503

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


STORAGE_UNAVAILABLE = "Хранилище аудио недоступно. Попробуйте позже или ответьте текстом."

NOT_A_VACANCY_RESUME = (
    "Похоже на резюме. Пришлите текст вакансии: требования работодателя к кандидату."
)
NOT_A_VACANCY_OTHER = (
    "Не похоже на вакансию. Пришлите текст вакансии целиком: должность, требования, стек."
)
URL_UNREADABLE = (
    "Не получилось прочитать вакансию по ссылке. "
    "Пришлите текст вакансии или сохранённую страницу (.html)."
)
URL_NOT_ALLOWED = (
    "Эту ссылку открыть нельзя. Пришлите текст вакансии или сохранённую страницу (.html)."
)
URL_FAKE_IP = (
    "Похоже, включён VPN в режиме fake-IP — через него ссылку не открыть. Пришлите текст вакансии. "
    "Чтобы ссылки работали, добавьте в .env строку FETCH_ALLOW_NETWORKS=240.0.0.0/4 "
    "и выполните docker compose up -d."
)
HTML_UNREADABLE = "Не получилось найти вакансию на странице. Пришлите текст вакансии."


def error_response(
    status_code: int, code: str, message: str, hint: str | None = None
) -> JSONResponse:
    """`hint` — что сделать человеку (008 §7); есть только у ошибок, которые он может исправить."""
    error = {"code": code, "message": message}
    if hint is not None:
        error["hint"] = hint
    return JSONResponse(status_code=status_code, content={"error": error})


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error(_: Request, exc: AppError) -> JSONResponse:
        return error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(LLMModelMissingError)
    async def llm_model_missing(_: Request, exc: LLMModelMissingError) -> JSONResponse:
        return error_response(503, "llm_model_missing", str(exc), exc.hint)

    @app.exception_handler(LLMUnavailableError)
    async def llm_unavailable(_: Request, exc: LLMUnavailableError) -> JSONResponse:
        return error_response(503, "llm_unavailable", str(exc), exc.hint)

    @app.exception_handler(LLMOutputError)
    async def llm_bad_output(_: Request, exc: LLMOutputError) -> JSONResponse:
        return error_response(502, "llm_bad_output", str(exc))

    @app.exception_handler(LLMError)
    async def llm_error(_: Request, exc: LLMError) -> JSONResponse:
        return error_response(502, "llm_error", str(exc), exc.hint)
