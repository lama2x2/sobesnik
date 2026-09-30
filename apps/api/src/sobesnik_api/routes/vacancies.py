import uuid

from fastapi import APIRouter

from sobesnik_api.db.models import Vacancy
from sobesnik_api.deps import DB, LLM, AppSettings, Fetcher
from sobesnik_api.schemas import VacancyCreate, VacancyOut, VacancyPatch
from sobesnik_api.services.vacancies import create_vacancy, get_vacancy, patch_vacancy

router = APIRouter(tags=["vacancies"])


@router.post("/vacancies", status_code=201, response_model=VacancyOut)
async def post_vacancy(
    body: VacancyCreate, db: DB, llm: LLM, fetcher: Fetcher, settings: AppSettings
) -> Vacancy:
    return await create_vacancy(db, llm, fetcher, body, max_retries=settings.llm_max_retries)


@router.get("/vacancies/{vacancy_id}", response_model=VacancyOut)
async def read_vacancy(vacancy_id: uuid.UUID, db: DB) -> Vacancy:
    return await get_vacancy(db, vacancy_id)


@router.patch("/vacancies/{vacancy_id}", response_model=VacancyOut)
async def update_vacancy(vacancy_id: uuid.UUID, body: VacancyPatch, db: DB) -> Vacancy:
    return await patch_vacancy(db, vacancy_id, body)
