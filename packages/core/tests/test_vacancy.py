import pytest
from pydantic import ValidationError

from sobesnik_core.topics import load_topics
from sobesnik_core.vacancy import (
    LEVELS,
    ParsedVacancy,
    VacancyProfile,
    check_parsed,
    normalize_profile,
    normalize_requirement_text,
    normalize_stack,
    parse_json_schema,
)

VALID = {
    "title": "Python-разработчик",
    "level": "middle",
    "stack": ["python", "fastapi"],
    "requirements": [{"text": "asyncio", "is_required": True, "topic": "python.asyncio"}],
}


def test_valid_profile() -> None:
    profile = VacancyProfile.model_validate(VALID)
    assert profile.level == "middle"
    assert profile.requirements[0].is_required
    assert profile.requirements[0].topic == "python.asyncio"


def test_topic_optional_in_model() -> None:
    data = {**VALID, "requirements": [{"text": "x", "is_required": False}]}
    assert VacancyProfile.model_validate(data).requirements[0].topic is None


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


def test_parsed_vacancy_kinds() -> None:
    assert ParsedVacancy.model_validate({"kind": "resume", "profile": None}).profile is None
    assert ParsedVacancy.model_validate({"kind": "other"}).profile is None
    with pytest.raises(ValidationError):
        ParsedVacancy.model_validate({"kind": "job"})


def test_parse_json_schema() -> None:
    catalog = load_topics()
    schema = parse_json_schema(catalog)
    assert list(schema["properties"]) == ["kind", "profile"]
    assert schema["properties"]["kind"]["enum"] == ["vacancy", "resume", "other"]
    requirement = schema["$defs"]["Requirement"]
    assert requirement["properties"]["topic"]["enum"] == list(catalog.leaf_slugs)
    assert "topic" in requirement["required"]
    level = schema["$defs"]["VacancyProfile"]["properties"]["level"]
    assert set(level["enum"]) == set(LEVELS)
    # модельная схема не испорчена
    assert "enum" not in str(ParsedVacancy.model_json_schema()["$defs"]["Requirement"])


def parsed(requirements: list[dict[str, object]]) -> ParsedVacancy:
    return ParsedVacancy.model_validate(
        {"kind": "vacancy", "profile": {**VALID, "requirements": requirements}}
    )


def test_check_ok() -> None:
    assert check_parsed(parsed(VALID["requirements"]), load_topics()) == []  # type: ignore[arg-type]


def test_check_non_vacancy_ignores_profile() -> None:
    assert check_parsed(ParsedVacancy(kind="resume"), load_topics()) == []


def test_check_vacancy_without_profile() -> None:
    assert check_parsed(ParsedVacancy(kind="vacancy"), load_topics())


def test_check_topics_and_duplicates() -> None:
    problems = check_parsed(
        parsed(
            [
                {"text": "SQL", "is_required": True, "topic": "db.sql"},
                {"text": "Kafka", "is_required": True, "topic": "arch"},
                {"text": "Go", "is_required": True},
                {"text": "  sql ", "is_required": False, "topic": "db.sql"},
            ]
        ),
        load_topics(),
    )
    assert len(problems) == 3
    assert "requirements.1.topic" in problems[0]
    assert "requirements.2.topic" in problems[1]
    assert "requirements.3" in problems[2]


def test_normalize_stack() -> None:
    aliases = {"k8s": "kubernetes", "postgres": "postgresql", "ci/cd": "ci-cd"}
    stack = [" Python", "K8s", "kubernetes", "Postgres", "CI/CD", "Spring  Boot", ""]
    assert normalize_stack(stack, aliases) == [
        "python",
        "kubernetes",
        "postgresql",
        "ci-cd",
        "spring boot",
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("индексы в postgresql", "Индексы в postgresql"),
        ("ёмкость очередей", "Ёмкость очередей"),
        ("Опыт с Kafka", "Опыт с Kafka"),
        ("iOS-разработка", "iOS-разработка"),
        ("gRPC  и   REST", "gRPC и REST"),
        ("", ""),
    ],
)
def test_normalize_requirement_text(text: str, expected: str) -> None:
    assert normalize_requirement_text(text) == expected


def test_normalize_profile() -> None:
    profile = VacancyProfile.model_validate(
        {**VALID, "stack": ["K8s"], "requirements": [{"text": "sql", "is_required": True}]}
    )
    normalized = normalize_profile(profile, load_topics())
    assert normalized.stack == ["kubernetes"]
    assert normalized.requirements[0].text == "sql"
    assert (
        normalize_profile(
            VacancyProfile.model_validate(
                {**VALID, "requirements": [{"text": "транзакции", "is_required": True}]}
            ),
            load_topics(),
        )
        .requirements[0]
        .text
        == "Транзакции"
    )
