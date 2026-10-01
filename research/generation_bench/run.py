"""Замер разбора вакансий и генерации вопросов на живых моделях Ollama (002 §13).

    docker compose run --rm tools uv run python research/generation_bench/run.py \\
        --models qwen3:8b qwen3.5:4b qwen3:1.7b [--quick]

Вызывает те же функции, что и API, но без БД. Результаты — в research/data/generation_bench/:
calls.csv (по вызову), questions.md (вопросы для ручной проверки), summary.md (сводка).
"""

import argparse
import asyncio
import csv
import json
import re
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from sobesnik_api.errors import UnprocessableError
from sobesnik_api.services.sessions import generate_questions
from sobesnik_api.services.vacancies import parse_vacancy
from sobesnik_api.sources.extract import extract_vacancy_text
from sobesnik_api.sources.fetch import PageFetcher
from sobesnik_core.checks import is_experience_question, question_problems
from sobesnik_core.questions import GeneratedQuestions
from sobesnik_core.topics import load_topics
from sobesnik_llm import (
    LLMConfig,
    LLMError,
    LLMHealth,
    LLMOutputError,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    clean_json_text,
    make_provider,
)

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
DATA = REPO / "research" / "data"
VACANCIES = DATA / "vacancies"
OUT = DATA / "generation_bench"
PLUS_RE = re.compile(
    r"плюсом|желательн|преимуществ|nice[- ]to[- ]have|would be a plus|is a plus", re.I
)


class RecordingProvider:
    """Обёртка над провайдером: запоминает все ответы модели."""

    def __init__(self, inner: LLMProvider) -> None:
        self.inner = inner
        self.name = inner.name
        self.model = inner.model
        self.responses: list[LLMResponse] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        response = await self.inner.generate(request)
        self.responses.append(response)
        return response

    async def health(self) -> LLMHealth:
        return await self.inner.health()

    async def aclose(self) -> None:
        await self.inner.aclose()

    def take(self) -> list[LLMResponse]:
        responses, self.responses = self.responses, []
        return responses


@dataclass
class Call:
    model: str
    item: str
    op: str  # parse | reject | generate
    ok: bool
    seconds: float
    attempts: int
    tokens_in: int
    tokens_out: int
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


async def load_vacancies(labels: dict[str, Any], refresh: bool) -> dict[str, str]:
    VACANCIES.mkdir(parents=True, exist_ok=True)
    fetcher = PageFetcher(timeout_s=30)
    texts = {}
    for item in labels["vacancies"]:
        path = VACANCIES / f"{item['id']}.txt"
        if refresh or not path.exists():
            page = await fetcher.fetch(item["url"])
            text = extract_vacancy_text(page.content, page.url, page.charset)
            if not text:
                raise SystemExit(f"не удалось извлечь {item['url']}")
            path.write_text(text, encoding="utf-8")
            await asyncio.sleep(1)
        texts[item["id"]] = path.read_text(encoding="utf-8")
    await fetcher.aclose()
    return texts


def failure(exc: Exception) -> str:
    if isinstance(exc, LLMOutputError):
        return f"LLMOutputError: {exc.problems[:3]}"
    return f"{type(exc).__name__}: {exc}"


def usage(responses: list[LLMResponse]) -> tuple[int, int]:
    return (
        sum(r.tokens_in or 0 for r in responses),
        sum(r.tokens_out or 0 for r in responses),
    )


def first_attempt_experience(responses: list[LLMResponse]) -> int:
    """Сколько вопросов про опыт было в первом ответе модели, до повторов."""
    if not responses:
        return 0
    try:
        first = GeneratedQuestions.model_validate_json(clean_json_text(responses[0].text))
    except ValueError:
        return 0
    return sum(is_experience_question(q.text) for q in first.questions)


async def bench_model(
    model: str,
    labels: dict[str, Any],
    texts: dict[str, str],
    *,
    base_url: str,
    num_ctx: int,
    n: int,
    questions_md: list[str],
) -> list[Call]:
    config = LLMConfig(
        provider="ollama", base_url=base_url, model=model, timeout_s=600, num_ctx=num_ctx
    )
    llm = RecordingProvider(make_provider(config))
    health = await llm.health()
    if not health.ok:
        raise SystemExit(f"{model}: {health.detail}")
    # прогрев: загрузка модели в память не должна попасть в замер
    await llm.generate(LLMRequest(system="Ответь одним словом.", prompt="Привет"))
    llm.take()

    calls: list[Call] = []
    catalog = load_topics()
    questions_md.append(f"\n## {model}\n")

    for item in labels["vacancies"]:
        vid = item["id"]
        started = time.perf_counter()
        try:
            parsed = await parse_vacancy(llm, texts[vid])
        except (UnprocessableError, LLMError) as exc:
            responses = llm.take()
            calls.append(
                Call(
                    model,
                    vid,
                    "parse",
                    False,
                    time.perf_counter() - started,
                    len(responses),
                    *usage(responses),
                    detail=failure(exc),
                )
            )
            print(f"  {vid}: разбор не удался — {failure(exc)}")
            continue
        responses = llm.take()
        profile = parsed.profile
        has_plus = bool(PLUS_RE.search(texts[vid]))
        extra: dict[str, Any] = {
            "repaired_topics": parsed.repaired_topics,
            "level": profile.level,
            "level_ok": profile.level in item["level"],
            "requirements": len(profile.requirements),
            "optional": sum(not r.is_required for r in profile.requirements),
            "has_plus": has_plus,
            "topics_ok": all(r.topic and catalog.is_leaf(r.topic) for r in profile.requirements),
            "general_topics": sum(
                bool(r.topic and r.topic.endswith(".general")) for r in profile.requirements
            ),
            "lowercase_requirements": sum(r.text[:1].islower() for r in profile.requirements),
        }
        calls.append(
            Call(
                model,
                vid,
                "parse",
                True,
                time.perf_counter() - started,
                len(responses),
                *usage(responses),
                extra=extra,
            )
        )
        print(
            f"  {vid}: разбор {calls[-1].seconds:.1f} с, уровень {profile.level}, "
            f"попыток {len(responses)}"
        )

        started = time.perf_counter()
        try:
            generated = await generate_questions(llm, profile, n)
        except LLMError as exc:
            responses = llm.take()
            calls.append(
                Call(
                    model,
                    vid,
                    "generate",
                    False,
                    time.perf_counter() - started,
                    len(responses),
                    *usage(responses),
                    detail=failure(exc),
                )
            )
            print(f"  {vid}: генерация не удалась — {failure(exc)}")
            continue
        responses = llm.take()
        questions = generated.questions
        extra = {
            "experience_first_try": first_attempt_experience(responses),
            "experience_final": sum(is_experience_question(q.text) for q in questions),
            "multi_question": sum(q.text.count("?") > 1 for q in questions),
            "key_points_avg": statistics.mean(len(q.key_points) for q in questions),
            "problems_final": len(question_problems(questions)),
        }
        calls.append(
            Call(
                model,
                vid,
                "generate",
                True,
                time.perf_counter() - started,
                len(responses),
                *usage(responses),
                extra=extra,
            )
        )
        print(f"  {vid}: генерация {calls[-1].seconds:.1f} с, попыток {len(responses)}")

        questions_md.append(f"\n### {vid} — {profile.title} ({profile.level})\n")
        for slot, q in zip(generated.slots, questions, strict=True):
            req = profile.requirements[slot.requirement_index]
            questions_md.append(f"- **{q.text}**  \n  _{req.text} · {req.topic}_")
            questions_md.extend(f"  - {kp}" for kp in q.key_points)

    for item in labels["non_vacancies"]:
        text = (HERE / "non_vacancies" / item["file"]).read_text(encoding="utf-8")
        started = time.perf_counter()
        try:
            parsed = await parse_vacancy(llm, text)
            ok, detail = False, f"принят как вакансия: {parsed.profile.title}"
        except UnprocessableError as exc:
            ok, detail = exc.code == "not_a_vacancy", exc.message
        except LLMError as exc:
            ok, detail = False, failure(exc)
        responses = llm.take()
        calls.append(
            Call(
                model,
                item["file"],
                "reject",
                ok,
                time.perf_counter() - started,
                len(responses),
                *usage(responses),
                detail=detail,
                extra={"expected": item["kind"]},
            )
        )
        print(f"  {item['file']}: {'отклонён' if ok else 'НЕ отклонён'} ({detail[:60]})")

    await llm.aclose()
    return calls


def median(values: list[float]) -> str:
    return f"{statistics.median(values):.1f}" if values else "—"


def summarize(calls: list[Call], models: list[str]) -> str:
    rows = [
        "| Метрика | " + " | ".join(models) + " |",
        "|---|" + "---|" * len(models),
    ]

    def row(name: str, fn: Any) -> None:
        rows.append(
            f"| {name} | "
            + " | ".join(fn([c for c in calls if c.model == m]) for m in models)
            + " |"
        )

    def parses(cs: list[Call]) -> list[Call]:
        return [c for c in cs if c.op == "parse"]

    def gens(cs: list[Call]) -> list[Call]:
        return [c for c in cs if c.op == "generate"]

    def ok_parses(cs: list[Call]) -> list[Call]:
        return [c for c in parses(cs) if c.ok]

    row("разбор: успешно", lambda cs: f"{len(ok_parses(cs))}/{len(parses(cs))}")
    row(
        "разбор: с первой попытки",
        lambda cs: f"{sum(c.attempts == 1 for c in ok_parses(cs))}/{len(parses(cs))}",
    )
    row(
        "разбор: медиана / макс, с",
        lambda cs: (
            f"{median([c.seconds for c in ok_parses(cs)])} / "
            f"{max((c.seconds for c in ok_parses(cs)), default=0):.1f}"
        ),
    )
    row(
        "уровень совпал",
        lambda cs: f"{sum(c.extra['level_ok'] for c in ok_parses(cs))}/{len(parses(cs))}",
    )
    row(
        "«будет плюсом» → is_required=false",
        lambda cs: (
            f"{sum(c.extra['optional'] > 0 for c in ok_parses(cs) if c.extra['has_plus'])}"
            f"/{sum(c.extra['has_plus'] for c in ok_parses(cs))}"
        ),
    )
    row(
        "темы `.general`, доля",
        lambda cs: (
            f"{sum(c.extra['general_topics'] for c in ok_parses(cs))}"
            f"/{sum(c.extra['requirements'] for c in ok_parses(cs))}"
        ),
    )
    row(
        "темы починены в `.general`",
        lambda cs: str(sum(c.extra["repaired_topics"] for c in ok_parses(cs))),
    )
    row(
        "требования со строчной буквы",
        lambda cs: str(sum(c.extra["lowercase_requirements"] for c in ok_parses(cs))),
    )
    row(
        "генерация 5 вопросов: успешно", lambda cs: f"{sum(c.ok for c in gens(cs))}/{len(gens(cs))}"
    )
    row(
        "генерация: с первой попытки",
        lambda cs: f"{sum(c.ok and c.attempts == 1 for c in gens(cs))}/{len(gens(cs))}",
    )
    row(
        "генерация: медиана / макс, с",
        lambda cs: (
            f"{median([c.seconds for c in gens(cs) if c.ok])} / "
            f"{max((c.seconds for c in gens(cs) if c.ok), default=0):.1f}"
        ),
    )
    row(
        "вопросы про опыт: до повторов / в итоге",
        lambda cs: (
            f"{sum(c.extra['experience_first_try'] for c in gens(cs) if c.ok)} / "
            f"{sum(c.extra['experience_final'] for c in gens(cs) if c.ok)}"
        ),
    )
    row(
        "вопросы с несколькими «?»",
        lambda cs: str(sum(c.extra["multi_question"] for c in gens(cs) if c.ok)),
    )
    row(
        "«не вакансия» отклонена",
        lambda cs: (
            f"{sum(c.ok for c in cs if c.op == 'reject')}/{sum(c.op == 'reject' for c in cs)}"
        ),
    )
    row(
        "токены вход/выход на разбор, медиана",
        lambda cs: (
            f"{median([c.tokens_in for c in ok_parses(cs)])} / "
            f"{median([c.tokens_out for c in ok_parses(cs)])}"
        ),
    )
    return "\n".join(rows)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", nargs="+", required=True)
    parser.add_argument("--base-url", default="http://host.docker.internal:11434")
    parser.add_argument("--num-ctx", type=int, default=16384)
    parser.add_argument("--n", type=int, default=5)
    parser.add_argument("--refresh", action="store_true", help="перекачать вакансии")
    parser.add_argument("--fetch-only", action="store_true")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="1 вакансия, 1 «не вакансия», 3 вопроса — проверка, что всё работает",
    )
    args = parser.parse_args()

    labels = yaml.safe_load((HERE / "labels.yaml").read_text(encoding="utf-8"))
    if args.quick:
        labels = {
            "vacancies": labels["vacancies"][:1],
            "non_vacancies": labels["non_vacancies"][:1],
        }
        args.n = 3
    texts = await load_vacancies(labels, args.refresh)
    for vid, text in texts.items():
        print(f"{vid}: {len(text)} символов, блок «плюсом»: {bool(PLUS_RE.search(text))}")
    if args.fetch_only:
        return

    OUT.mkdir(parents=True, exist_ok=True)
    questions_md = ["# Вопросы для ручной проверки"]
    calls: list[Call] = []
    for model in args.models:
        print(f"== {model}")
        calls += await bench_model(
            model,
            labels,
            texts,
            base_url=args.base_url,
            num_ctx=args.num_ctx,
            n=args.n,
            questions_md=questions_md,
        )
        # промежуточные результаты — на случай, если следующая модель упадёт
        write_results(calls, args.models, questions_md)
    write_results(calls, args.models, questions_md)
    print((OUT / "summary.md").read_text(encoding="utf-8"))


def write_results(calls: list[Call], models: list[str], questions_md: list[str]) -> None:
    with (OUT / "calls.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "model",
                "item",
                "op",
                "ok",
                "seconds",
                "attempts",
                "tokens_in",
                "tokens_out",
                "detail",
                "extra",
            ]
        )
        for c in calls:
            d = asdict(c)
            writer.writerow(
                [
                    d["model"],
                    d["item"],
                    d["op"],
                    d["ok"],
                    f"{d['seconds']:.1f}",
                    d["attempts"],
                    d["tokens_in"],
                    d["tokens_out"],
                    d["detail"],
                    json.dumps(d["extra"], ensure_ascii=False),
                ]
            )
    present = [m for m in models if any(c.model == m for c in calls)]
    (OUT / "summary.md").write_text(summarize(calls, present) + "\n", encoding="utf-8")
    (OUT / "questions.md").write_text("\n".join(questions_md) + "\n", encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
