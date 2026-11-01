"""Прогоны: переоценка набора ответов заданной моделью (EVAL-6, 003 §9.1)."""

import logging
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any

from sqlalchemy import ColumnElement, and_, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sobesnik_api.db.models import Answer, EvalRun, Evaluation
from sobesnik_api.errors import ConflictError, NotFoundError
from sobesnik_api.services.evaluation import EVAL_PROMPT, EvalConfig, evaluate_and_store
from sobesnik_core.prompt import load_prompt
from sobesnik_core.rubric import load_rubric
from sobesnik_core.versioned import version_number
from sobesnik_llm import LLMProvider, LLMUnavailableError

log = logging.getLogger(__name__)

MAX_UNAVAILABLE_IN_ROW = 3
"""Столько ответов подряд без LLM — и прогон останавливается со статусом failed."""


@dataclass(frozen=True)
class RunFilter:
    session_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    since: date | None = None
    until: date | None = None
    """Включительно: ответы до конца этого дня (UTC)."""

    def to_json(self) -> dict[str, str]:
        return {k: str(v) for k, v in self.__dict__.items() if v is not None}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "RunFilter":
        return cls(
            session_id=uuid.UUID(data["session_id"]) if data.get("session_id") else None,
            user_id=uuid.UUID(data["user_id"]) if data.get("user_id") else None,
            since=date.fromisoformat(data["since"]) if data.get("since") else None,
            until=date.fromisoformat(data["until"]) if data.get("until") else None,
        )

    def conditions(self) -> list[ColumnElement[bool]]:
        conds: list[ColumnElement[bool]] = [
            Answer.text.is_not(None),
            Answer.status.not_in(("received", "evaluating")),
        ]
        if self.session_id:
            conds.append(Answer.session_id == self.session_id)
        if self.user_id:
            conds.append(Answer.user_id == self.user_id)
        if self.since:
            conds.append(Answer.created_at >= datetime.combine(self.since, time.min, UTC))
        if self.until:
            end = datetime.combine(self.until, time.max, UTC)
            conds.append(Answer.created_at <= end)
        return conds


def run_config(run: EvalRun, *, max_retries: int, num_ctx: int | None) -> EvalConfig:
    """Рубрика и промпт ровно тех версий, что записаны в прогоне; иначе ConflictError."""
    rubric = load_rubric(version_number(run.rubric_version))
    template = load_prompt(EVAL_PROMPT, version_number(run.prompt_version))
    if (rubric.version, template.version) != (run.rubric_version, run.prompt_version):
        raise ConflictError(
            "run_config_changed",
            f"рубрика или промпт изменились с момента создания прогона: было "
            f"{run.rubric_version} / {run.prompt_version}, стало "
            f"{rubric.version} / {template.version}. Создайте новый прогон.",
        )
    return EvalConfig(rubric, template, max_retries, num_ctx)


async def create_run(
    db: AsyncSession,
    *,
    name: str,
    provider: str,
    model: str,
    run_filter: RunFilter,
    rubric_number: int | None = None,
) -> EvalRun:
    rubric = load_rubric(rubric_number)
    template = load_prompt(EVAL_PROMPT)
    run = EvalRun(
        name=name,
        provider=provider,
        model=model,
        rubric_version=rubric.version,
        prompt_version=template.version,
        filter=run_filter.to_json(),
        status="queued",
    )
    db.add(run)
    await db.commit()
    return run


async def get_run(db: AsyncSession, run_id: uuid.UUID) -> EvalRun:
    run = await db.get(EvalRun, run_id)
    if run is None:
        raise NotFoundError("eval_run_not_found", "прогон не найден")
    return run


def _done_in_run(run_id: uuid.UUID) -> ColumnElement[bool]:
    return exists().where(
        Evaluation.answer_id == Answer.id,
        Evaluation.eval_run_id == run_id,
        Evaluation.status == "done",
    )


async def run_answer_ids(db: AsyncSession, run: EvalRun) -> list[uuid.UUID]:
    """Ответы прогона, у которых ещё нет успешной оценки этого прогона."""
    conds = RunFilter.from_json(run.filter).conditions()
    result = await db.scalars(
        select(Answer.id)
        .where(and_(*conds), ~_done_in_run(run.id))
        .order_by(Answer.created_at, Answer.id)
    )
    return list(result)


async def refresh_counters(db: AsyncSession, run: EvalRun) -> None:
    conds = RunFilter.from_json(run.filter).conditions()
    total = await db.scalar(select(func.count()).select_from(Answer).where(and_(*conds)))
    done = await db.scalar(
        select(func.count()).select_from(Answer).where(and_(*conds), _done_in_run(run.id))
    )
    failed_only = await db.scalar(
        select(func.count())
        .select_from(Answer)
        .where(
            and_(*conds),
            ~_done_in_run(run.id),
            exists().where(Evaluation.answer_id == Answer.id, Evaluation.eval_run_id == run.id),
        )
    )
    run.total, run.done, run.failed = total or 0, done or 0, failed_only or 0


def _unavailable_row(
    llm: LLMProvider, config: EvalConfig, answer_id: uuid.UUID, run_id: uuid.UUID
) -> Evaluation:
    return Evaluation(
        answer_id=answer_id,
        eval_run_id=run_id,
        status="failed",
        rubric_version=config.rubric.version,
        prompt_version=config.template.version,
        provider=llm.name,
        model=llm.model,
        params=config.params,
        attempts=0,
        latency_ms=0,
        error="llm_unavailable",
    )


async def execute_run(
    sessionmaker: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    make_llm: Callable[[EvalRun], AbstractAsyncContextManager[LLMProvider]],
    *,
    max_retries: int = 2,
    num_ctx: int | None = None,
) -> EvalRun:
    """Оценивает ответы прогона по одному; каждая оценка — отдельный коммит.

    `make_llm` открывает провайдер с моделью прогона.
    """
    async with sessionmaker() as db:
        run = await get_run(db, run_id)
        try:
            config = run_config(run, max_retries=max_retries, num_ctx=num_ctx)
        except ConflictError as exc:
            run.status, run.error, run.finished_at = "failed", exc.message, datetime.now(UTC)
            await db.commit()
            return run
        run.status, run.error, run.finished_at = "running", None, None
        await refresh_counters(db, run)
        await db.commit()
        answer_ids = await run_answer_ids(db, run)

    log.info("eval run started", extra={"eval_run_id": str(run_id), "answers": len(answer_ids)})
    unavailable_in_row = 0
    error: str | None = None
    async with make_llm(run) as llm:
        for answer_id in answer_ids:
            async with sessionmaker() as db:
                answer = await db.get(Answer, answer_id)
                if answer is None:  # удалён, пока шёл прогон
                    continue
                try:
                    await evaluate_and_store(db, llm, config, answer, run_id=run_id)
                    unavailable_in_row = 0
                except LLMUnavailableError as exc:
                    db.add(_unavailable_row(llm, config, answer.id, run_id))
                    unavailable_in_row += 1
                    error = f"LLM недоступна: {exc}"
                run = await get_run(db, run_id)
                await refresh_counters(db, run)
                await db.commit()
            if unavailable_in_row >= MAX_UNAVAILABLE_IN_ROW:
                break

    async with sessionmaker() as db:
        run = await get_run(db, run_id)
        await refresh_counters(db, run)
        stopped = unavailable_in_row >= MAX_UNAVAILABLE_IN_ROW
        run.status = "failed" if stopped else "done"
        run.error = error if stopped else None
        run.finished_at = datetime.now(UTC)
        await db.commit()
    log.info(
        "eval run finished",
        extra={
            "eval_run_id": str(run_id),
            "status": run.status,
            "done": run.done,
            "failed": run.failed,
        },
    )
    return run
