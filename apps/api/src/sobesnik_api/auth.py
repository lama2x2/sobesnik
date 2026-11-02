"""Доступ к API (006 §5): сервисный токен для клиентов (бот) и админ-токен для `/admin/*`.

Токен передаётся заголовком `Authorization: Bearer <token>`. Не задан сервисный токен — проверки
нет; не задан админ-токен — служебные эндпоинты выключены (`404`).
"""

import hmac

from fastapi import Request
from pydantic import SecretStr

from sobesnik_api.errors import AppError, NotFoundError
from sobesnik_api.settings import Settings


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def _matches(given: str | None, expected: SecretStr) -> bool:
    return given is not None and hmac.compare_digest(
        given.encode(), expected.get_secret_value().encode()
    )


def require_service_token(request: Request) -> None:
    settings: Settings = request.app.state.settings
    expected = settings.api_service_token
    if expected is not None and not _matches(_bearer(request), expected):
        raise UnauthorizedError("нужен сервисный токен: Authorization: Bearer …")


def require_admin_token(request: Request) -> None:
    settings: Settings = request.app.state.settings
    expected = settings.admin_token
    if expected is None:
        raise NotFoundError("not_found", "служебные эндпоинты выключены: ADMIN_TOKEN не задан")
    if not _matches(_bearer(request), expected):
        raise UnauthorizedError("нужен админ-токен: Authorization: Bearer …")
