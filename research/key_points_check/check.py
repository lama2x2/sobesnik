"""Замер проверки пунктов эталона (011 §6.4): находит ли модель подложенные ошибки и как часто
тревожится зря.

    docker compose run --rm -e LLM_PROVIDER=ollama tools \
        uv run python research/key_points_check/check.py [--quick | --live FILE]
    ollama stop qwen3.5:4b

Набор — `cases.yaml` рядом: 10 вопросов по 4 верных пункта, у каждого одна подложенная ошибка.
Вопросы идут пачками по 5, как в сессии, тем же кодом, что в продукте (`check_questions`):
1. «чистый» прогон — все пункты верные, любое сомнение — ложная тревога;
2. «с ошибками» — в каждом вопросе один пункт заменён; найден — если отмечен именно он.
`--quick` — только первая пачка (5 вопросов), два вызова модели.

`--live FILE` — частота сомнений на сгенерированных вопросах из БД (JSON-выгрузка
`[{"text", "key_points"}]`, только локально в `research/data/`): разметки нет, только доля.
Результат — `research/data/key_points_check/*.json`, тексты сомнений — только там.
"""

import argparse
import asyncio
import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from sobesnik_api.services.key_points_check import check_questions
from sobesnik_api.settings import Settings
from sobesnik_llm import LLMProvider, make_provider

HERE = Path(__file__).resolve().parent
OUT = HERE.parents[1] / "research" / "data" / "key_points_check"
BATCH = 5

Question = tuple[str, list[str]]


def load_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = yaml.safe_load((HERE / "cases.yaml").read_text())["cases"]
    return cases


def corrupted(case: dict[str, Any]) -> Question:
    points = list(case["points"])
    points[case["wrong"]["n"] - 1] = case["wrong"]["text"]
    return case["question"], points


def batches[T](items: Sequence[T], size: int) -> list[Sequence[T]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


async def run(llm: LLMProvider, questions: Sequence[Question]) -> tuple[list[Any], list[int]]:
    """Сомнения по вопросам и время каждого вызова, мс."""
    issues: list[Any] = []
    latencies: list[int] = []
    for batch in batches(questions, BATCH):
        started = time.perf_counter()
        result = await check_questions(llm, batch)
        latencies.append(int((time.perf_counter() - started) * 1000))
        issues += result.issues
    return issues, latencies


async def measure(llm: LLMProvider, cases: list[dict[str, Any]]) -> dict[str, Any]:
    clean_q: list[Question] = [(c["question"], list(c["points"])) for c in cases]
    clean, clean_ms = await run(llm, clean_q)
    dirty, dirty_ms = await run(llm, [corrupted(c) for c in cases])

    false_clean = sum(len(i) for i in clean)
    pairs = list(zip(cases, dirty, strict=True))
    found = sum(any(x["n"] == c["wrong"]["n"] for x in i) for c, i in pairs)
    false_dirty = sum(
        sum(x["n"] != c["wrong"]["n"] for x in i) for c, i in zip(cases, dirty, strict=True)
    )
    points = sum(len(c["points"]) for c in cases)
    return {
        "questions": len(cases),
        "injected_found": found,
        "false_alarms_clean": false_clean,
        "clean_points": points,
        "false_alarms_dirty": false_dirty,
        "dirty_correct_points": points - len(cases),
        "questions_with_false_alarm": sum(bool(i) for i in clean),
        "missed_kinds": [
            c["wrong"]["kind"]
            for c, i in zip(cases, dirty, strict=True)
            if not any(x["n"] == c["wrong"]["n"] for x in i)
        ],
        "latency_ms": clean_ms + dirty_ms,
        "issues": {"clean": clean, "dirty": dirty},
    }


async def live(llm: LLMProvider, rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [r for r in rows if r.get("key_points")]
    questions: list[Question] = [(r["text"], list(r["key_points"])) for r in rows]
    issues, latencies = await run(llm, questions)
    return {
        "questions": len(questions),
        "points": sum(len(p) for _, p in questions),
        "doubts": sum(len(i) for i in issues),
        "questions_with_doubts": sum(bool(i) for i in issues),
        "latency_ms": latencies,
        "issues": [
            {"question": q, "key_points": p, "issues": i}
            for (q, p), i in zip(questions, issues, strict=True)
            if i
        ],
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--quick", action="store_true", help="одна пачка из 5 вопросов")
    parser.add_argument("--live", type=Path, help="JSON со сгенерированными вопросами из БД")
    args = parser.parse_args()

    settings = Settings()
    llm = make_provider(settings.llm_config("eval"))
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        if args.live:
            report = await live(llm, json.loads(args.live.read_text()))
            name = "live"
            print(
                f"вопросов {report['questions']}, пунктов {report['points']}: сомнений "
                f"{report['doubts']} в {report['questions_with_doubts']} вопросах; "
                f"вызовы, с: {[round(ms / 1000, 1) for ms in report['latency_ms']]}"
            )
        else:
            cases = load_cases()[:BATCH] if args.quick else load_cases()
            report = await measure(llm, cases)
            name = "quick" if args.quick else "cases"
            print(
                f"подложенных найдено: {report['injected_found']} из {report['questions']}\n"
                f"ложных тревог на чистых: {report['false_alarms_clean']} из "
                f"{report['clean_points']} пунктов ({report['questions_with_false_alarm']} "
                f"вопросов из {report['questions']})\n"
                f"ложных тревог рядом с ошибкой: {report['false_alarms_dirty']} из "
                f"{report['dirty_correct_points']}\n"
                f"не найдены: {report['missed_kinds']}\n"
                f"вызовы, с: {[round(ms / 1000, 1) for ms in report['latency_ms']]}"
            )
        report["model"] = settings.llm_config("eval").model
        (OUT / f"{name}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        await llm.aclose()


if __name__ == "__main__":
    asyncio.run(main())
