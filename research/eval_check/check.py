"""Быстрая живая проверка оценки (003 §11): несколько минут, без сети.

    docker compose up -d --build
    docker compose run --rm tools uv run python research/eval_check/check.py
    ollama stop qwen3.5:4b

Через API: вакансия из кэша research/data/vacancies/ → сессия из 2 вопросов → «хороший» ответ
(ключевые пункты, связанные в текст) и «не знаю» → оценки воркером. Затем прогон той же моделью
по сессии: баллы должны совпасть с «боевыми» (temperature 0, тот же seed). С `--other-model` —
ещё прогон другой моделью и CSV с оценками рядом.
"""

import argparse
import asyncio
import os
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from sobesnik_api.db.session import make_engine, make_sessionmaker
from sobesnik_api.export import ExportFilter, select_rows, write_csv
from sobesnik_api.queue import ArqQueue
from sobesnik_api.services.eval_runs import RunFilter, create_run, get_run
from sobesnik_api.settings import Settings


def auth_headers() -> dict[str, str]:
    """Сервисный токен API (006 §5.1), если он задан в env."""
    token = os.environ.get("API_SERVICE_TOKEN", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


ROOT = Path(__file__).resolve().parents[2]
VACANCIES = ROOT / "research" / "data" / "vacancies"
OUT = ROOT / "research" / "data" / "eval_check"
BAD_ANSWER = "Не знаю, не сталкивался с этим."
POLL_S = 2.0


def good_answer(key_points: list[str]) -> str:
    return "Если коротко: " + " ".join(p.rstrip(".") + "." for p in key_points)


async def wait_answer(api: httpx.AsyncClient, answer_id: str, timeout_s: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while True:
        body: dict[str, Any] = (await api.get(f"/answers/{answer_id}")).json()
        if body["status"] in ("done", "failed") or time.monotonic() > deadline:
            return body
        await asyncio.sleep(POLL_S)


def print_evaluation(title: str, body: dict[str, Any]) -> None:
    print(f"\n{title}: статус {body['status']}, ошибка {body['error']}")
    evaluation = body.get("evaluation")
    if not evaluation:
        return
    print(
        f"  итог {evaluation['total']}, пройден {evaluation['passed']}, "
        f"{evaluation['latency_ms'] / 1000:.1f} с, модель {evaluation['model']}"
    )
    for score in evaluation["scores"]:
        print(f"  {score['criterion']}: {score['score']} — {score['reason']}")
    print(f"  упущено: {evaluation['missed_points']}")
    print(f"  совет: {evaluation['advice']}")


async def run_and_wait(
    settings: Settings, session_id: uuid.UUID, model: str, name: str, timeout_s: float
) -> uuid.UUID:
    engine = make_engine(settings.database_url)
    queue = ArqQueue(settings.redis_url)
    try:
        maker = make_sessionmaker(engine)
        async with maker() as db:
            run = await create_run(
                db,
                name=name,
                provider=settings.llm_config("eval").provider,
                model=model,
                run_filter=RunFilter(session_id=session_id),
            )
        await queue.enqueue_run(run.id)
        deadline = time.monotonic() + timeout_s
        while True:
            async with maker() as db:
                run = await get_run(db, run.id)
            if run.status in ("done", "failed") or time.monotonic() > deadline:
                break
            await asyncio.sleep(POLL_S)
        print(f"\nпрогон {name} ({model}): {run.status}, готово {run.done}/{run.total}")
        return run.id
    finally:
        await queue.aclose()
        await engine.dispose()


async def scores_by_run(
    settings: Settings, session_id: uuid.UUID, run_ids: list[uuid.UUID], csv_path: Path
) -> dict[tuple[uuid.UUID, uuid.UUID | None], dict[str, Any]]:
    engine = make_engine(settings.database_url)
    try:
        async with make_sessionmaker(engine)() as db:
            rows = await select_rows(
                db, ExportFilter(run_ids=run_ids, live=True, session_id=session_id)
            )
    finally:
        await engine.dispose()
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as out:
        write_csv(out, rows, "wide")
    print(f"\nCSV: {csv_path.relative_to(ROOT)}")
    return {(r.answer.id, r.evaluation.eval_run_id): r.evaluation.scores or {} for r in rows}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://api:8000/api/v1")
    parser.add_argument(
        "--vacancy", default="python_backend", help="файл из research/data/vacancies"
    )
    parser.add_argument("--other-model", help="ещё один прогон другой моделью, например qwen3:1.7b")
    parser.add_argument("--timeout", type=float, default=300, help="ожидание одной оценки, с")
    args = parser.parse_args()
    settings = Settings()
    failures: list[str] = []

    async with httpx.AsyncClient(base_url=args.api, timeout=300, headers=auth_headers()) as api:
        health = (await api.get("/health/llm")).json()
        eval_model = health["roles"]["eval"]["model"]
        print("eval:", health["roles"]["eval"])
        user = (await api.put("/users/telegram/900002")).json()["id"]
        text = (VACANCIES / f"{args.vacancy}.txt").read_text(encoding="utf-8")

        started = time.perf_counter()
        vacancy = await api.post("/vacancies", json={"user_id": user, "text": text})
        vacancy.raise_for_status()
        print(f"разбор вакансии: {time.perf_counter() - started:.0f} с")
        started = time.perf_counter()
        response = await api.post("/sessions", json={"vacancy_id": vacancy.json()["id"], "n": 3})
        response.raise_for_status()
        session = response.json()
        print(f"3 вопроса: {time.perf_counter() - started:.0f} с")
        session_id = uuid.UUID(session["id"])
        good_q, bad_q = session["questions"][0], session["questions"][1]

        answers = {}
        for title, question, answer in (
            ("хороший ответ", good_q, good_answer(good_q["key_points"])),
            ("«не знаю»", bad_q, BAD_ANSWER),
        ):
            print(f"\n{title}\n  вопрос: {question['text']}\n  ответ: {answer}")
            response = await api.post(
                f"/sessions/{session['id']}/questions/{question['id']}/answer",
                json={"text": answer},
            )
            answers[title] = response.json()["answer_id"]
        results = {}
        for title, answer_id in answers.items():
            results[title] = await wait_answer(api, answer_id, args.timeout)
            print_evaluation(title, results[title])

    good, bad = (r.get("evaluation") for r in results.values())
    if not good or not bad:
        failures.append("не все ответы оценены")
    else:
        if not (good["total"] >= 60 and good["passed"]):
            failures.append(f"хороший ответ: итог {good['total']}, ожидалось ≥ 60 и пройден")
        if not (bad["total"] < 30 and not bad["passed"]):
            failures.append(f"«не знаю»: итог {bad['total']}, ожидалось < 30 и не пройден")

    same = await run_and_wait(settings, session_id, eval_model, "повтор", args.timeout * 2)
    runs = [same]
    if args.other_model:
        runs.append(
            await run_and_wait(settings, session_id, args.other_model, "другая", args.timeout * 2)
        )
    scores = await scores_by_run(settings, session_id, runs, OUT / f"{session_id}.csv")
    for answer_id in answers.values():
        live = scores.get((uuid.UUID(answer_id), None))
        repeat = scores.get((uuid.UUID(answer_id), same))
        if live != repeat:
            failures.append(f"{answer_id}: повторная оценка не совпала с «боевой»")

    print("\nПРОВАЛЫ:" if failures else "\nВсе проверки пройдены.")
    for failure in failures:
        print(" -", failure)


if __name__ == "__main__":
    asyncio.run(main())
