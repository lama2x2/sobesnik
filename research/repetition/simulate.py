"""Симуляция: правило «слабая тема» (PRG-2) против SM-2 под шумом оценщика (011 §5.5).

Ещё два ориентира: «раз в неделю» — все темы с интервалом 7 дней, и «SM-2 по двум» — SM-2, в который
вместо балла попытки идёт среднее её и предыдущей (можно ли сгладить шум без смены алгоритма).

    docker compose run --rm tools uv run python research/repetition/simulate.py
    docker compose run --rm tools uv run python research/repetition/simulate.py --runs 50 --days 30

Без LLM и БД, секунды на CPU. Выбор тем и сроки — тем же кодом, что в продукте
(`sobesnik_core.progress`, `sobesnik_core.repetition`); старое правило восстановлено здесь как было
до спринта 11.

Модель ученика (допущения, а не замер на людях):
- у темы «истинное знание» 0–100; в день 0 каждая тема отвечается один раз (тренировка по вакансии);
- балл попытки = знание + шум оценщика N(0, σ), обрезанный в 0–100;
- попытка учит: знание += 30 % от недостающего до 100 (ответ, оценка, упущенное, совет);
- знание забывается по экспоненте к уровню 20 с «устойчивостью» в днях: у новой темы — от 1
  до 9 дней (одну тему помнишь неделю, другую забываешь за день), после каждой попытки
  устойчивость × 1,8 (чем больше повторений, тем медленнее забывается);
- в день — не больше одного повторения по 5 тем; повторять нечего — человек не занимается.

Метрики к концу срока: среднее знание, сколько тем «забыто» (знание < 60), сколько всего
повторений и сколько из них «впустую» — по теме, которую человек и так знал (знание ≥ 80
в момент повторения).
"""

import argparse
import math
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sobesnik_core.progress import (
    Thresholds,
    TopicStats,
    is_low,
    repeat_topics,
    stats_from_history,
    topic_status,
)
from sobesnik_core.repetition import schedule

T = Thresholds()
START = datetime(2026, 1, 1, 9, tzinfo=UTC)
FLOOR = 20.0
LEARN = 0.3
STABILITY_RANGE = (1.0, 9.0)
STABILITY_GROWTH = 1.8
SESSION = 5
FORGOTTEN_BELOW = 60
KNOWN_FROM = 80


@dataclass
class Topic:
    knowledge: float
    stability: float
    last_at: datetime = START
    history: list[tuple[int, datetime]] = field(default_factory=list)
    """Попытки, новые первыми — как их отдаёт БД."""

    def at(self, now: datetime) -> float:
        days = (now - self.last_at).total_seconds() / 86400
        return FLOOR + (self.knowledge - FLOOR) * math.exp(-days / self.stability)

    def attempt(self, now: datetime, sigma: float, rng: random.Random) -> None:
        current = self.at(now)
        score = round(min(100.0, max(0.0, rng.gauss(current, sigma))))
        self.history.insert(0, (score, now))
        self.knowledge = current + LEARN * (100 - current)
        self.stability *= STABILITY_GROWTH
        self.last_at = now

    def stats(self) -> TopicStats:
        return stats_from_history(self.history)

    def smoothed_due(self, now: datetime) -> bool:
        """SM-2 по среднему попытки и предыдущей."""
        oldest_first = list(reversed(self.history))
        smoothed = [
            ((score + oldest_first[i - 1][0]) // 2 if i else score, at)
            for i, (score, at) in enumerate(oldest_first)
        ]
        sched = schedule(smoothed)
        return sched is None or sched.due(now)


def rule_topics(progress: dict[str, TopicStats], limit: int) -> list[str]:
    """Правило до спринта 11: слабые от самых низких, потом «мало данных» с низким баллом."""
    weak = [s for s, st in progress.items() if topic_status(st, T) == "weak"]
    low = [s for s, st in progress.items() if is_low(st, T)]

    def by_avg(slug: str) -> float:
        return progress[slug].avg_last3 or 0.0

    return (sorted(weak, key=by_avg) + sorted(low, key=by_avg))[:limit]


POLICIES = (
    ("rule", "слабая тема"),
    ("weekly", "раз в неделю"),
    ("sm2", "SM-2"),
    ("sm2-avg2", "SM-2 по двум"),
)


@dataclass
class Result:
    knowledge: float
    forgotten: float
    repetitions: float
    wasted: float


def simulate(policy: str, sigma: float, days: int, n_topics: int, seed: int) -> Result:
    rng = random.Random(seed)
    topics = {
        f"t{i}": Topic(knowledge=rng.uniform(20, 75), stability=rng.uniform(*STABILITY_RANGE))
        for i in range(n_topics)
    }
    for topic in topics.values():
        topic.attempt(START, sigma, rng)
    repetitions = wasted = 0
    for day in range(1, days + 1):
        now = START + timedelta(days=day)
        progress = {slug: t.stats() for slug, t in topics.items()}
        if policy == "rule":
            chosen = rule_topics(progress, SESSION)
        elif policy == "weekly":
            chosen = [s for s, t in topics.items() if now - t.last_at >= timedelta(days=7)]
            chosen = sorted(chosen, key=lambda s: topics[s].last_at)[:SESSION]
        elif policy == "sm2-avg2":
            due = [s for s, t in topics.items() if t.smoothed_due(now)]
            chosen = sorted(due, key=lambda s: progress[s].avg_last3 or 0.0)[:SESSION]
        else:
            chosen = repeat_topics(progress, T, now, SESSION)
        for slug in chosen:
            topic = topics[slug]
            wasted += topic.at(now) >= KNOWN_FROM
            topic.attempt(now, sigma, rng)
            repetitions += 1
    end = START + timedelta(days=days)
    final = [t.at(end) for t in topics.values()]
    return Result(
        knowledge=sum(final) / len(final),
        forgotten=sum(k < FORGOTTEN_BELOW for k in final),
        repetitions=repetitions,
        wasted=wasted,
    )


def average(results: list[Result]) -> Result:
    n = len(results)
    return Result(
        knowledge=sum(r.knowledge for r in results) / n,
        forgotten=sum(r.forgotten for r in results) / n,
        repetitions=sum(r.repetitions for r in results) / n,
        wasted=sum(r.wasted for r in results) / n,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--runs", type=int, default=200, help="прогонов на ячейку (seed 0…N−1)")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--topics", type=int, default=10)
    parser.add_argument("--sigma", type=float, nargs="+", default=[0.0, 10.0, 20.0])
    args = parser.parse_args()

    print(f"{args.topics} тем, {args.days} дней, {args.runs} прогонов на строку\n")
    print("| σ шума | Правило | Знание в конце | Забыто тем | Повторений | Из них впустую |")
    print("|---|---|---|---|---|---|")
    for sigma in args.sigma:
        for policy, name in POLICIES:
            runs = [
                simulate(policy, sigma, args.days, args.topics, seed) for seed in range(args.runs)
            ]
            r = average(runs)
            print(
                f"| {sigma:g} | {name} | {r.knowledge:.1f} | {r.forgotten:.1f} из {args.topics}"
                f" | {r.repetitions:.1f} | {r.wasted:.1f} |"
            )


if __name__ == "__main__":
    main()
