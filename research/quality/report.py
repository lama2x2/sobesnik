"""Отчёт о качестве оценки по выгрузкам CSV (005 §11): таблица «модель × метрика» и график Δ от WER.

    docker compose run --rm tools uv run python research/quality/report.py \
        research/data/quality/*.csv -o research/data/quality/report.md

Читает только CSV (широкий формат `sobesnik-cli export`), поэтому из одних и тех же файлов
всегда получается один и тот же отчёт. Рядом с отчётом пишется `delta_wer.png`, если в CSV
есть пары прогонов одной модели по `asr` и `reference`.
"""

import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

from sobesnik_core.quality import (
    Pair,
    bootstrap_ci,
    mae,
    qwk,
    round_half_up,
    spearman,
    to_percent,
    to_scale,
    wer,
)

Row = dict[str, str]


@dataclass
class Run:
    key: str
    name: str
    model: str
    prompt: str
    temperature: str
    seed: str
    transcript: str
    datasets: set[str] = field(default_factory=set)
    rows: list[Row] = field(default_factory=list)

    @property
    def done(self) -> list[Row]:
        return [r for r in self.rows if r["status"] == "done"]

    def totals(self) -> dict[str, float]:
        return {r["answer_id"]: float(r["total"]) for r in self.done}

    @property
    def config(self) -> tuple[str, ...]:
        return (self.model, self.prompt, self.temperature, self.transcript, *sorted(self.datasets))


def read_rows(paths: Iterable[Path]) -> list[Row]:
    rows: dict[str, Row] = {}
    for path in paths:
        with path.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                rows[row["evaluation_id"]] = row
    return list(rows.values())


def group_runs(rows: Sequence[Row]) -> list[Run]:
    runs: dict[str, Run] = {}
    for row in rows:
        key = row["run_id"] or "live"
        if key not in runs:
            runs[key] = Run(
                key=key,
                name=row["run_name"] or "боевые",
                model=row["model"],
                prompt=row["prompt_version"].split("#")[0],
                temperature=row["temperature"],
                seed=row["seed"],
                transcript=row["transcript_kind"],
            )
        run = runs[key]
        run.rows.append(row)
        if row["dataset"]:
            run.datasets.add(row["dataset"])
    return sorted(runs.values(), key=lambda r: (sorted(r.datasets), r.name))


def fmt(value: float, digits: int = 2) -> str:
    return "—" if math.isnan(value) else f"{value:.{digits}f}"


def fmt_ci(value: float, ci: tuple[float, float]) -> str:
    if math.isnan(value):
        return "—"
    return f"{value:.2f} [{fmt(ci[0])}; {fmt(ci[1])}]"


def _rho(pairs: Sequence[Pair]) -> float:
    return spearman([a for a, _ in pairs], [b for _, b in pairs])


def _qwk_of(low: float, high: float) -> Callable[[list[Pair]], float]:
    def metric(pairs: list[Pair]) -> float:
        return qwk(
            [to_scale(a, low, high) for a, _ in pairs],
            [round_half_up(b) for _, b in pairs],
            int(low),
            int(high),
        )

    return metric


def human_pairs(run: Run) -> tuple[list[Pair], float, float] | None:
    """(итог модели, оценка человека) по ответам с человеческой шкалой."""
    rows = [r for r in run.done if r["human_score"] and r["human_score_max"]]
    rows = [r for r in rows if not is_synthetic(r)]
    if not rows:
        return None
    high = float(rows[0]["human_score_max"])
    return [(float(r["total"]), float(r["human_score"])) for r in rows], 0.0, high


def is_synthetic(row: Row) -> bool:
    return "level" in json.loads(row["human_scores"] or "{}")


def ladder(run: Run) -> tuple[float, float] | None:
    """Доля вопросов с правильным порядком 3 > 2 > max(0) и Спирмен итога с уровнем."""
    rows = [r for r in run.done if r["human_scores"] and is_synthetic(r)]
    if not rows:
        return None
    by_question: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        by_question[r["question_id"]][int(float(r["human_score"]))].append(float(r["total"]))
    ordered = [
        min(lv[3]) > max(lv[2]) > max(lv[0])
        for lv in by_question.values()
        if lv.get(3) and lv.get(2) and lv.get(0)
    ]
    share = sum(ordered) / len(ordered) if ordered else math.nan
    rho = spearman([float(r["total"]) for r in rows], [float(r["human_score"]) for r in rows])
    return share, rho


def run_table(runs: Sequence[Run]) -> list[str]:
    lines = [
        "| прогон | модель | промпт | t | данные | готово | сбоев | медиана, с "
        "| ρ с людьми [95 % ДИ] | QWK [95 % ДИ] | MAE (0–100) | лестница: порядок | ρ с уровнем |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for run in runs:
        done = run.done
        failed = len(run.rows) - len(done)
        latency = (
            statistics.median(float(r["latency_ms"]) for r in done) / 1000 if done else math.nan
        )
        rho = qk = err = math.nan
        rho_ci = qk_ci = (math.nan, math.nan)
        human = human_pairs(run)
        if human:
            pairs, low, high = human
            rho, rho_ci = _rho(pairs), bootstrap_ci(pairs, _rho)
            metric = _qwk_of(low, high)
            qk, qk_ci = metric(pairs), bootstrap_ci(pairs, metric)
            err = mae([a for a, _ in pairs], [to_percent(b, low, high) for _, b in pairs])
        lad = ladder(run) or (math.nan, math.nan)
        data = ", ".join(sorted(run.datasets)) + (f" ({run.transcript})" if run.transcript else "")
        lines.append(
            f"| {run.name} | {run.model} | {run.prompt} | {run.temperature} | {data or '—'} "
            f"| {len(done)} | {failed / len(run.rows):.0%} | {fmt(latency, 1)} "
            f"| {fmt_ci(rho, rho_ci)} | {fmt_ci(qk, qk_ci)} | {fmt(err, 1)} "
            f"| {fmt(lad[0])} | {fmt(lad[1])} |"
        )
    return lines


def human_vs_human(rows: Sequence[Row]) -> list[str]:
    """Потолок: насколько согласуются между собой люди-оценщики датасета."""
    lines = []
    by_dataset: dict[str, dict[str, Row]] = defaultdict(dict)
    for r in rows:
        if r["dataset"] and r["human_scores"] and not is_synthetic(r):
            by_dataset[r["dataset"]][r["dataset_item"]] = r
    for dataset, items in sorted(by_dataset.items()):
        scores = [json.loads(r["human_scores"]) for r in items.values()]
        pairs = [
            (s["grader_1"], s["grader_2"]) for s in scores if "grader_1" in s and "grader_2" in s
        ]
        if len(pairs) < 2:
            continue
        high = float(next(iter(items.values()))["human_score_max"])
        a, b = [x for x, _ in pairs], [y for _, y in pairs]
        k = qwk([round_half_up(x) for x in a], [round_half_up(y) for y in b], 0, int(high))
        e = mae([to_percent(x, 0, high) for x in a], [to_percent(y, 0, high) for y in b])
        lines.append(
            f"- **{dataset}**, человек × человек на тех же {len(pairs)} ответах: "
            f"ρ = {fmt(spearman(a, b))}, QWK = {fmt(k)}, MAE = {fmt(e, 1)}"
        )
    return lines


def between_models(runs: Sequence[Run]) -> list[str]:
    lines = [
        "| прогон A | прогон B | ответов | ρ | совпадение «пройден» |",
        "|---|---|---|---|---|",
    ]
    candidates = [r for r in runs if r.temperature in ("0", "0.0") and r.datasets]
    for a, b in combinations(candidates, 2):
        if a.datasets != b.datasets or a.transcript != b.transcript or a.model == b.model:
            continue
        ta, tb = a.totals(), b.totals()
        common = sorted(set(ta) & set(tb))
        if len(common) < 2:
            continue
        pa = {r["answer_id"]: r["passed"] for r in a.done}
        pb = {r["answer_id"]: r["passed"] for r in b.done}
        same = sum(pa[i] == pb[i] for i in common) / len(common)
        rho = spearman([ta[i] for i in common], [tb[i] for i in common])
        lines.append(f"| {a.name} | {b.name} | {len(common)} | {fmt(rho)} | {same:.0%} |")
    return lines if len(lines) > 2 else []


def stability(runs: Sequence[Run]) -> list[str]:
    groups: dict[tuple[str, ...], list[Run]] = defaultdict(list)
    for run in runs:
        groups[run.config].append(run)
    lines = []
    for config, group in groups.items():
        if len(group) < 2:
            continue
        totals = [g.totals() for g in group]
        common = sorted(set.intersection(*(set(t) for t in totals)))
        if not common:
            continue
        names = ", ".join(g.name for g in group)
        if config[2] in ("0", "0.0"):
            same = sum(len({t[i] for t in totals}) == 1 for i in common) / len(common)
            lines.append(
                f"- `{config[0]}`, t = 0, {len(group)} прогона ({names}): итог совпал "
                f"у {same:.0%} из {len(common)} ответов"
            )
        else:
            spread = statistics.fmean(statistics.pstdev([t[i] for t in totals]) for i in common)
            lines.append(
                f"- `{config[0]}`, t = {config[2]}, {len(group)} прогона с разными seed ({names}): "
                f"среднее стандартное отклонение итога {spread:.1f} балла на {len(common)} ответах"
            )
    return lines


@dataclass(frozen=True)
class AsrPoint:
    answer: str
    delta: float
    wer: float


def asr_pairs(runs: Sequence[Run]) -> list[tuple[Run, Run, list[AsrPoint]]]:
    result = []
    for a in runs:
        if a.transcript != "asr":
            continue
        for b in runs:
            if b.transcript != "reference" or (a.model, a.prompt) != (b.model, b.prompt):
                continue
            if a.datasets != b.datasets:
                continue
            ref = {r["answer_id"]: r for r in b.done}
            points = [
                AsrPoint(
                    r["dataset_item"] or r["answer_id"],
                    float(r["total"]) - float(ref[r["answer_id"]]["total"]),
                    wer(ref[r["answer_id"]]["answer"], r["answer"]),
                )
                for r in a.done
                if r["answer_id"] in ref
            ]
            if points:
                result.append((a, b, points))
    return result


def asr_section(pairs: Sequence[tuple[Run, Run, list[AsrPoint]]]) -> list[str]:
    lines = []
    for a, b, points in pairs:
        deltas = [p.delta for p in points]
        wers = [p.wer for p in points]
        mean_abs = statistics.fmean(abs(d) for d in deltas)
        rho = spearman([abs(d) for d in deltas], wers)
        lines += [
            f"**{a.model}, промпт {a.prompt}: {a.name} (asr) против {b.name} (reference)**, "
            f"{len(points)} ответов: средняя |Δ| = {mean_abs:.1f}, "
            f"средняя Δ = {statistics.fmean(deltas):+.1f}, "
            f"средний WER = {statistics.fmean(wers):.0%}, ρ(|Δ|, WER) = {fmt(rho)}",
            "",
            "| фрагмент | WER | Δ итога |",
            "|---|---|---|",
            *(
                f"| {p.answer} | {p.wer:.0%} | {p.delta:+.0f} |"
                for p in sorted(points, key=lambda p: p.answer)
            ),
            "",
        ]
    return lines


def plot(pairs: Sequence[tuple[Run, Run, list[AsrPoint]]], path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(6, 4))
    for a, _, points in pairs:
        ax.scatter(
            [p.wer * 100 for p in points], [p.delta for p in points], label=f"{a.model} {a.prompt}"
        )
    ax.axhline(0, color="grey", linewidth=0.8)
    ax.set_xlabel("WER, % (small против large-v3)")
    ax.set_ylabel("Δ итога: asr − reference")
    ax.set_title("Влияние распознавания на оценку")
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def doubts_section(runs: Sequence[Run]) -> list[str]:
    lines = []
    for run in runs:
        if run.transcript != "asr":
            continue
        doubts = [d for r in run.done for d in r["asr_doubts"].split(" | ") if d]
        lines.append(
            f"- **{run.name}** ({run.model}, {run.prompt}): {len(doubts)} сомнений "
            f"на {len(run.done)} ответов"
        )
        lines += [f"  - {d}" for d in doubts]
    return lines


def wrong_points(rows: Sequence[Row]) -> str:
    with_points = [r for r in rows if r["status"] == "done" and r["key_points"]]
    if not with_points:
        return "Оценок вопросов с ключевыми пунктами в выгрузке нет."
    wrong = sum(bool(r["wrong_points"]) for r in with_points)
    return (
        f"Непустые `wrong_points` у {wrong} из {len(with_points)} оценок вопросов "
        f"с ключевыми пунктами ({wrong / len(with_points):.0%})."
    )


def build_report(rows: Sequence[Row], png: Path | None) -> str:
    runs = group_runs(rows)
    out: list[str] = ["# Качество оценки: отчёт", ""]
    out += ["## Модель × метрика", "", *run_table(runs), ""]
    out += [
        "ρ — Спирмен итога 0–100 с оценкой людей, QWK — по категориям шкалы датасета (итог × "
        "шкала / 100, округление), MAE — в баллах 0–100 (005 §11.1). «Лестница» — доля вопросов "
        "русского банка, где эталон > половина эталона > чужой ответ и «не знаю».",
        "",
    ]
    hh = human_vs_human(rows)
    if hh:
        out += ["## Ориентир: люди между собой", "", *hh, ""]
    bm = between_models(runs)
    if bm:
        out += ["## Согласие моделей между собой", "", *bm, ""]
    st = stability(runs)
    if st:
        out += ["## Стабильность", "", *st, ""]
    pairs = asr_pairs(runs)
    if pairs:
        out += ["## Влияние распознавания", "", *asr_section(pairs)]
        if png is not None:
            plot(pairs, png)
            out += [f"График: `{png.name}`.", ""]
    dbt = doubts_section(runs)
    if dbt:
        out += ["## Сомнения в распознавании", "", *dbt, ""]
    out += ["## wrong_points", "", wrong_points(rows), ""]
    return "\n".join(out)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("csv", nargs="+", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args(argv)
    rows = read_rows(args.csv)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = build_report(rows, args.output.with_name("delta_wer.png"))
    args.output.write_text(report, encoding="utf-8")
    print(f"{args.output}: {len(rows)} оценок")


if __name__ == "__main__":
    main()
