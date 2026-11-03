"""Вход в веб-кабинет (009 §5, §9): ссылки из бота, локальный вход, текущий пользователь, выход."""

from fastapi import APIRouter, Response

from sobesnik_api.auth import CallerDep
from sobesnik_api.db.models import User
from sobesnik_api.deps import DB, AppSettings
from sobesnik_api.errors import NotFoundError
from sobesnik_api.schemas import LinkCreate, LinkOut, LinkRedeem, MeOut, TokenOut
from sobesnik_api.services.auth import create_link, local_login, redeem_link, revoke

router = APIRouter(prefix="/auth", tags=["auth"])

public_router = APIRouter(prefix="/auth", tags=["auth"])
"""Без токена: секрет ссылки или код и есть доступ."""


@router.post("/links", status_code=201)
async def post_link(body: LinkCreate, caller: CallerDep, db: DB) -> LinkOut:
    caller.require_service()
    link = await create_link(db, body.user_id)
    return LinkOut(secret=link.secret, code=link.code, expires_at=link.expires_at)


@public_router.post("/links/redeem")
async def post_redeem(body: LinkRedeem, db: DB, settings: AppSettings) -> TokenOut:
    issued = await redeem_link(
        db, secret=body.secret, code=body.code, days=settings.web_session_days
    )
    return TokenOut(token=issued.token, user_id=issued.user_id, expires_at=issued.expires_at)


@router.post("/local")
async def post_local(caller: CallerDep, db: DB, settings: AppSettings) -> TokenOut:
    caller.require_service()
    if not settings.web_local_login:
        raise NotFoundError("local_login_disabled", "вход без Telegram выключен: WEB_LOCAL_LOGIN")
    issued = await local_login(db, settings.web_session_days)
    return TokenOut(token=issued.token, user_id=issued.user_id, expires_at=issued.expires_at)


@router.get("/me")
async def read_me(caller: CallerDep, db: DB) -> MeOut:
    user = await db.get(User, caller.require_user())
    assert user is not None  # токен удаляется вместе с пользователем
    return MeOut(user_id=user.id, telegram=user.telegram_id is not None, local=user.local)


@router.delete("/session", status_code=204)
async def logout(caller: CallerDep, db: DB) -> Response:
    await revoke(db, caller.require_user(), caller.token_id)
    return Response(status_code=204)


@router.delete("/sessions", status_code=204)
async def logout_everywhere(caller: CallerDep, db: DB) -> Response:
    await revoke(db, caller.require_user())
    return Response(status_code=204)
