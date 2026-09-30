from fastapi import APIRouter, Response
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from sobesnik_api.db.models import User
from sobesnik_api.deps import DB
from sobesnik_api.schemas import UserOut

router = APIRouter(tags=["users"])


@router.put("/users/telegram/{telegram_id}", response_model=UserOut)
async def upsert_telegram_user(telegram_id: int, db: DB, response: Response) -> User:
    created = await db.scalar(
        insert(User)
        .values(telegram_id=telegram_id)
        .on_conflict_do_nothing(index_elements=[User.telegram_id])
        .returning(User.id)
    )
    await db.commit()
    user = await db.scalar(select(User).where(User.telegram_id == telegram_id))
    assert user is not None
    response.status_code = 201 if created is not None else 200
    return user
