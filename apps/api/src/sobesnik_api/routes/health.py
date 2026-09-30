from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from sobesnik_api.deps import DB, Roles

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(db: DB) -> JSONResponse:
    try:
        await db.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError):
        return JSONResponse(status_code=503, content={"status": "unavailable", "db": "unavailable"})
    return JSONResponse(content={"status": "ok", "db": "ok"})


@router.get("/health/llm")
async def health_llm(roles: Roles) -> JSONResponse:
    results = await roles.health()
    ok = all(r.ok for r in results.values())
    content = {
        "status": "ok" if ok else "unavailable",
        "roles": {
            role: {
                "ok": r.ok,
                "provider": r.provider,
                "model": r.model,
                "detail": r.detail,
            }
            for role, r in results.items()
        },
    }
    return JSONResponse(status_code=200 if ok else 503, content=content)
