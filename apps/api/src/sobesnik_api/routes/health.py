from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from sobesnik_api.deps import DB, LLM

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(db: DB) -> JSONResponse:
    try:
        await db.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError):
        return JSONResponse(status_code=503, content={"status": "unavailable", "db": "unavailable"})
    return JSONResponse(content={"status": "ok", "db": "ok"})


@router.get("/health/llm")
async def health_llm(llm: LLM) -> JSONResponse:
    result = await llm.health()
    content = {
        "status": "ok" if result.ok else "unavailable",
        "provider": result.provider,
        "model": result.model,
    }
    if not result.ok:
        content["detail"] = result.detail or ""
    return JSONResponse(status_code=200 if result.ok else 503, content=content)
