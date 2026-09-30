"""Ошибки приложения и их HTTP-представление: {"error": {"code", "message"}}."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from sobesnik_llm import LLMError, LLMUnavailableError


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


class LLMBadOutputError(AppError):
    """Ответ модели не прошёл валидацию."""

    status_code = 502
    code = "llm_bad_output"


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content={"error": {"code": code, "message": message}}
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error(_: Request, exc: AppError) -> JSONResponse:
        return error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(LLMUnavailableError)
    async def llm_unavailable(_: Request, exc: LLMUnavailableError) -> JSONResponse:
        return error_response(503, "llm_unavailable", str(exc))

    @app.exception_handler(LLMError)
    async def llm_error(_: Request, exc: LLMError) -> JSONResponse:
        return error_response(502, "llm_error", str(exc))
