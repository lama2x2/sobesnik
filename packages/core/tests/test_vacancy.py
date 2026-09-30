import pytest
from pydantic import ValidationError

from sobesnik_core.vacancy import LEVELS, VacancyProfile

VALID = {
    "title": "Python-разработчик",
    "level": "middle",
    "stack": ["python", "fastapi"],
    "requirements": [{"text": "asyncio", "is_required": True}],
}


def test_valid_profile() -> None:
    profile = VacancyProfile.model_validate(VALID)
    assert profile.level == "middle"
    assert profile.requirements[0].is_required


@pytest.mark.parametrize("level", LEVELS)
def test_all_levels_accepted(level: str) -> None:
    assert VacancyProfile.model_validate({**VALID, "level": level}).level == level


@pytest.mark.parametrize(
    "patch",
    [
        {"level": "staff"},
        {"stack": []},
        {"stack": ["x"] * 31},
        {"requirements": []},
        {"requirements": [{"text": "x", "is_required": True}] * 16},
        {"title": ""},
        {"extra": 1},
    ],
)
def test_invalid_profile(patch: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        VacancyProfile.model_validate({**VALID, **patch})


def test_schema_has_level_enum() -> None:
    schema = VacancyProfile.model_json_schema()
    assert set(schema["properties"]["level"]["enum"]) == set(LEVELS)
