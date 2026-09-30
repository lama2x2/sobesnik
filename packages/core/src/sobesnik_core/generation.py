"""Сборка промптов разбора и генерации из доменных данных (002 §8–9)."""

import json
from collections.abc import Sequence

from sobesnik_core.limits import ASKED_QUESTIONS_IN_PROMPT
from sobesnik_core.planning import PlanSlot
from sobesnik_core.prompt import PromptTemplate, RenderedPrompt
from sobesnik_core.topics import TopicCatalog
from sobesnik_core.vacancy import VacancyProfile


def parse_prompt(template: PromptTemplate, catalog: TopicCatalog, text: str) -> RenderedPrompt:
    return template.render(topics=catalog.prompt_list(), vacancy_text=text)


def format_plan(profile: VacancyProfile, slots: Sequence[PlanSlot]) -> str:
    lines = []
    for number, slot in enumerate(slots, start=1):
        req = profile.requirements[slot.requirement_index]
        mark = "обязательно" if req.is_required else "желательно"
        topic = f" (тема: {req.topic})" if req.topic else ""
        aspect = f", аспект {slot.aspect}" if slot.aspect > 1 else ""
        lines.append(f"{number}. [{mark}{aspect}] {req.text}{topic}")
    return "\n".join(lines)


def format_asked(asked: Sequence[str]) -> str:
    recent = list(asked)[-ASKED_QUESTIONS_IN_PROMPT:]
    if not recent:
        return ""
    items = "\n".join(f"- {q}" for q in recent)
    return (
        f"\nЭти вопросы уже задавались по вакансии — не повторяй и не перефразируй их:\n{items}\n"
    )


def questions_prompt(
    template: PromptTemplate,
    profile: VacancyProfile,
    slots: Sequence[PlanSlot],
    asked: Sequence[str] = (),
) -> RenderedPrompt:
    return template.render(
        title=profile.title,
        level=profile.level,
        stack=json.dumps(profile.stack, ensure_ascii=False),
        n=str(len(slots)),
        plan=format_plan(profile, slots),
        asked=format_asked(asked),
    )
