from fastapi import APIRouter

from sobesnik_api.db.models import Vacancy
from sobesnik_api.deps import DB, LLM
from sobesnik_api.schemas import VacancyCreate, VacancyOut
from sobesnik_api.services.vacancies import create_vacancy

router = APIRouter(tags=["vacancies"])


@router.post("/vacancies", status_code=201, response_model=VacancyOut)
async def post_vacancy(body: VacancyCreate, db: DB, llm: LLM) -> Vacancy:
    return await create_vacancy(db, llm, body.user_id, body.text)
