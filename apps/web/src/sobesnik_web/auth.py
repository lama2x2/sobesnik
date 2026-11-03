"""Кто вошёл (009 §5): токен API лежит в cookie браузера, веб передаёт его в API как есть.

Своего состояния у веба нет: cookie без токена или с отозванным токеном — страница входа.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request, Response

from sobesnik_web.api import Api, ApiError, Gate, Me
from sobesnik_web.settings import WebSettings

COOKIE = "sobesnik_session"
CODE_FREE_TRIES = 5
CODE_PAUSE_S = 5.0


class LoginRequired(Exception):
    """Нет входа или он истёк — на страницу входа."""


@dataclass(frozen=True)
class Current:
    api: Api
    me: Me


async def current(request: Request) -> Current:
    token = request.cookies.get(COOKIE)
    if not token:
        raise LoginRequired
    gate: Gate = request.app.state.gate
    api = gate.for_user(token)
    try:
        me = await api.me()
    except ApiError as exc:
        if exc.status in (401, 403):
            raise LoginRequired from exc
        raise
    return Current(api=api, me=me)


User = Annotated[Current, Depends(current)]


def set_login(response: Response, token: str, settings: WebSettings) -> None:
    response.set_cookie(
        COOKIE,
        token,
        max_age=settings.web_session_days * 24 * 3600,
        httponly=True,
        samesite="lax",
        path="/",
    )


def clear_login(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/", httponly=True, samesite="lax")


class CodeThrottle:
    """Ввод кода с телефона (009 §5.2): после 5 неверных кодов подряд — не чаще раза в 5 с.

    Один счётчик на весь веб: он открыт только на этом компьютере.
    """

    def __init__(
        self,
        free_tries: int = CODE_FREE_TRIES,
        pause_s: float = CODE_PAUSE_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.free_tries = free_tries
        self.pause_s = pause_s
        self.clock = clock
        self.failures = 0
        self.last_try = float("-inf")

    def allowed(self) -> bool:
        if self.failures < self.free_tries:
            return True
        return self.clock() - self.last_try >= self.pause_s

    def record(self, ok: bool) -> None:
        self.last_try = self.clock()
        self.failures = 0 if ok else self.failures + 1
