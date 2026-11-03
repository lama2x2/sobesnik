"""Доступ к API (006 §5, 009 §5.1): сервисный токен (бот, веб для локального входа), токен
пользователя (веб) и админ-токен для `/admin/*`.

Токен передаётся заголовком `Authorization: Bearer <token>`. Не задан сервисный токен — запрос
без токена считается сервисным, как раньше; не задан админ-токен — служебные эндпоинты выключены
(`404`).
С токеном пользователя доступны только его вакансии, сессии, ответы и прогресс: чужое — `404`,
как будто его нет.
"""

import hmac
import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from pydantic import SecretStr

from sobesnik_api.deps import DB
from sobesnik_api.errors import AppError, NotFoundError
from sobesnik_api.services.auth import TOKEN_PREFIX, resolve_session
from sobesnik_api.settings import Settings


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"


@dataclass(frozen=True)
class Caller:
    """Кто вызывает API: сервис (`user_id is None`) или конкретный пользователь по токену."""

    user_id: uuid.UUID | None = None
    token_id: uuid.UUID | None = None

    @property
    def is_service(self) -> bool:
        return self.user_id is None

    def require_service(self) -> None:
        if not self.is_service:
            raise ForbiddenError("только для сервисного токена")

    def require_user(self) -> uuid.UUID:
        if self.user_id is None:
            raise ForbiddenError("нужен токен пользователя")
        return self.user_id

    def owns(self, owner_id: uuid.UUID | None) -> bool:
        return self.is_service or owner_id == self.user_id

    def check_user(self, user_id: uuid.UUID) -> None:
        """`/users/{id}/…` с токеном пользователя — только свой id."""
        if not self.owns(user_id):
            raise NotFoundError("user_not_found", "пользователь не найден")

    def user_for(self, user_id: uuid.UUID | None) -> uuid.UUID | None:
        """`user_id` из тела: с токеном пользователя его можно не передавать, чужой — `404`."""
        if self.user_id is None:
            return user_id
        if user_id is not None and user_id != self.user_id:
            raise NotFoundError("user_not_found", "пользователь не найден")
        return self.user_id


async def get_caller(request: Request, db: DB) -> Caller:
    settings: Settings = request.app.state.settings
    given = _bearer(request)
    expected = settings.api_service_token
    if given is not None and given.startswith(TOKEN_PREFIX):
        session = await resolve_session(db, given, settings.web_session_days)
        if session is None:
            raise UnauthorizedError("сессия истекла или отозвана: войдите снова")
        return Caller(user_id=session.user_id, token_id=session.id)
    if expected is not None and not _matches(given, expected):
        raise UnauthorizedError("нужен сервисный токен: Authorization: Bearer …")
    return Caller()


CallerDep = Annotated[Caller, Depends(get_caller)]


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def _matches(given: str | None, expected: SecretStr) -> bool:
    return given is not None and hmac.compare_digest(
        given.encode(), expected.get_secret_value().encode()
    )


def require_admin_token(request: Request) -> None:
    settings: Settings = request.app.state.settings
    expected = settings.admin_token
    if expected is None:
        raise NotFoundError("not_found", "служебные эндпоинты выключены: ADMIN_TOKEN не задан")
    if not _matches(_bearer(request), expected):
        raise UnauthorizedError("нужен админ-токен: Authorization: Bearer …")
