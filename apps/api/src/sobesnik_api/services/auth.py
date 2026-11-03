"""Вход в веб-кабинет (009 §5): одноразовые ссылки из бота, локальный вход и токены пользователя.

В БД лежат только SHA-256 секретов. Токен пользователя начинается с `sbu_`: по префиксу API
отличает его от сервисного и не принимает просроченный токен за «запрос без токена».
"""

import hashlib
import logging
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sobesnik_api.db.models import AuthToken, User
from sobesnik_api.errors import NotFoundError

log = logging.getLogger(__name__)

TOKEN_PREFIX = "sbu_"
LINK_TTL = timedelta(minutes=10)
TOUCH_EVERY = timedelta(hours=1)
"""Как часто продлевать веб-сессию: не пишем в БД на каждый запрос."""

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
"""Без I, L, O, 0 и 1 — их путают, когда переписывают с телефона."""
CODE_LENGTH = 8

LINK_INVALID = "ссылка или код недействительны: возьмите новую командой /web в боте"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def normalize_code(code: str) -> str:
    """`k7mq-2xda`, `K7MQ 2XDA` и `K7MQ2XDA` — один код."""
    return re.sub(r"[^0-9A-Z]", "", code.upper())


def _new_code() -> str:
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    return f"{code[:4]}-{code[4:]}"


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Link:
    secret: str
    code: str
    expires_at: datetime


@dataclass(frozen=True)
class IssuedToken:
    token: str
    user_id: uuid.UUID
    expires_at: datetime


@dataclass(frozen=True)
class SessionToken:
    id: uuid.UUID
    user_id: uuid.UUID


async def create_link(db: AsyncSession, user_id: uuid.UUID) -> Link:
    """Новая ссылка гасит прошлые неиспользованные ссылки пользователя (009 §5.2)."""
    if await db.get(User, user_id) is None:
        raise NotFoundError("user_not_found", "пользователь не найден")
    now = _now()
    await db.execute(
        delete(AuthToken).where(
            AuthToken.kind == "link",
            or_(
                AuthToken.user_id == user_id,
                AuthToken.expires_at < now,
                AuthToken.used_at.is_not(None),
            ),
        )
    )
    secret = secrets.token_urlsafe(32)
    code = _new_code()
    link = AuthToken(
        user_id=user_id,
        kind="link",
        token_hash=_hash(secret),
        code_hash=_hash(normalize_code(code)),
        expires_at=now + LINK_TTL,
    )
    db.add(link)
    await db.commit()
    log.info("login link issued", extra={"user_id": str(user_id)})
    return Link(secret=secret, code=code, expires_at=link.expires_at)


async def _issue_session(db: AsyncSession, user_id: uuid.UUID, days: int) -> IssuedToken:
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    now = _now()
    row = AuthToken(
        user_id=user_id,
        kind="session",
        token_hash=_hash(token),
        expires_at=now + timedelta(days=days),
        last_used_at=now,
    )
    db.add(row)
    await db.commit()
    return IssuedToken(token=token, user_id=user_id, expires_at=row.expires_at)


async def redeem_link(
    db: AsyncSession, *, secret: str | None, code: str | None, days: int
) -> IssuedToken:
    """Погасить ссылку или код и выдать токен. Повтор, срок и «нет такой» — одна ошибка."""
    if secret:
        condition = AuthToken.token_hash == _hash(secret)
    elif code and len(normalize_code(code)) == CODE_LENGTH:
        condition = AuthToken.code_hash == _hash(normalize_code(code))
    else:
        raise NotFoundError("link_invalid", LINK_INVALID)
    now = _now()
    user_id = await db.scalar(
        update(AuthToken)
        .where(
            condition,
            AuthToken.kind == "link",
            AuthToken.used_at.is_(None),
            AuthToken.expires_at > now,
        )
        .values(used_at=now)
        .returning(AuthToken.user_id)
    )
    if user_id is None:
        await db.rollback()
        log.info("login link rejected", extra={"by": "secret" if secret else "code"})
        raise NotFoundError("link_invalid", LINK_INVALID)
    issued = await _issue_session(db, user_id, days)
    log.info("login by link", extra={"user_id": str(user_id), "by": "secret" if secret else "code"})
    return issued


async def local_login(db: AsyncSession, days: int) -> IssuedToken:
    """Найти или создать локального пользователя — один на инстанс (009 §5.3)."""
    await db.execute(
        insert(User)
        .values(local=True)
        .on_conflict_do_nothing(index_elements=[User.local], index_where=text("local"))
    )
    user_id = await db.scalar(select(User.id).where(User.local))
    assert user_id is not None
    issued = await _issue_session(db, user_id, days)
    log.info("local login", extra={"user_id": str(user_id)})
    return issued


async def resolve_session(db: AsyncSession, token: str, days: int) -> SessionToken | None:
    """Действующая веб-сессия по токену; продлевается на `days` с последнего захода."""
    now = _now()
    row = await db.scalar(
        select(AuthToken).where(
            AuthToken.token_hash == _hash(token),
            AuthToken.kind == "session",
            AuthToken.revoked_at.is_(None),
            AuthToken.expires_at > now,
        )
    )
    if row is None:
        return None
    if row.last_used_at is None or now - row.last_used_at > TOUCH_EVERY:
        row.last_used_at = now
        row.expires_at = now + timedelta(days=days)
        await db.commit()
    return SessionToken(id=row.id, user_id=row.user_id)


async def revoke(db: AsyncSession, user_id: uuid.UUID, token_id: uuid.UUID | None = None) -> int:
    """Отозвать одну веб-сессию или, без `token_id`, все сессии пользователя («Выйти везде»)."""
    query = update(AuthToken).where(
        AuthToken.user_id == user_id,
        AuthToken.kind == "session",
        AuthToken.revoked_at.is_(None),
    )
    if token_id is not None:
        query = query.where(AuthToken.id == token_id)
    result = await db.execute(query.values(revoked_at=_now()).returning(AuthToken.id))
    revoked = len(result.all())
    await db.commit()
    log.info("web sessions revoked", extra={"user_id": str(user_id), "count": revoked})
    return revoked
