import uuid

from fastapi import APIRouter

from sobesnik_api.auth import Caller, CallerDep
from sobesnik_api.db.models import Vacancy
from sobesnik_api.deps import DB, LLM, AppSettings, Fetcher
from sobesnik_api.errors import NotFoundError, UnprocessableError
from sobesnik_api.schemas import VacancyCreate, VacancyOut, VacancyPatch
from sobesnik_api.services.vacancies import create_vacancy, get_vacancy, patch_vacancy

router = APIRouter(tags=["vacancies"])


async def owned_vacancy(db: DB, caller: Caller, vacancy_id: uuid.UUID) -> Vacancy:
    """Вакансия, если она своя; чужая — `404`, как несуществующая (009 §5.1)."""
    vacancy = await get_vacancy(db, vacancy_id)
    if not caller.owns(vacancy.user_id):
        raise NotFoundError("vacancy_not_found", "вакансия не найдена")
    return vacancy


@router.post("/vacancies", status_code=201, response_model=VacancyOut)
async def post_vacancy(
    body: VacancyCreate,
    db: DB,
    llm: LLM,
    fetcher: Fetcher,
    settings: AppSettings,
    caller: CallerDep,
) -> Vacancy:
    body.user_id = caller.user_for(body.user_id)
    if body.user_id is None:
        raise UnprocessableError("user_required", "нужен user_id")
    return await create_vacancy(db, llm, fetcher, body, max_retries=settings.llm_max_retries)


@router.get("/vacancies/{vacancy_id}", response_model=VacancyOut)
async def read_vacancy(vacancy_id: uuid.UUID, db: DB, caller: CallerDep) -> Vacancy:
    return await owned_vacancy(db, caller, vacancy_id)


@router.patch("/vacancies/{vacancy_id}", response_model=VacancyOut)
async def update_vacancy(
    vacancy_id: uuid.UUID, body: VacancyPatch, db: DB, caller: CallerDep
) -> Vacancy:
    await owned_vacancy(db, caller, vacancy_id)
    return await patch_vacancy(db, vacancy_id, body)
