"""Приёмка спринта 2 через настоящий API (002 §16, критерии 1–3 и 5).

    docker compose up -d --build
    docker compose run --rm tools uv run python research/generation_bench/api_check.py

Идёт в api по сети compose: вакансии по ссылкам hh.ru, тексты «не вакансия», нерабочая ссылка,
сохранённая HTML-страница и /health/llm. Печатает итог по каждому критерию и время запросов.
"""

import argparse
import asyncio
import statistics
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

HERE = Path(__file__).resolve().parent


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://api:8000/api/v1")
    parser.add_argument("--limit", type=int, default=None, help="сколько вакансий проверить")
    parser.add_argument("--quick", action="store_true", help="1 вакансия и 1 «не вакансия»")
    args = parser.parse_args()
    labels = yaml.safe_load((HERE / "labels.yaml").read_text(encoding="utf-8"))
    if args.quick:
        labels = {
            "vacancies": labels["vacancies"][:1],
            "non_vacancies": labels["non_vacancies"][:1],
        }
        args.n = 3
    else:
        args.n = 5
    failures: list[str] = []
    timings: dict[str, list[float]] = {"vacancy": [], "session": []}

    async with httpx.AsyncClient(base_url=args.api, timeout=900) as api:
        health = (await api.get("/health/llm")).json()
        print("health/llm:", health)
        if health.get("status") != "ok" or set(health.get("roles", {})) != {"gen", "eval"}:
            failures.append("health/llm: нет обеих ролей или статус не ok")

        user = (await api.put("/users/telegram/900001")).json()["id"]

        for item in labels["vacancies"][: args.limit]:
            started = time.perf_counter()
            response = await api.post("/vacancies", json={"user_id": user, "url": item["url"]})
            timings["vacancy"].append(time.perf_counter() - started)
            if response.status_code != 201:
                failures.append(
                    f"{item['id']}: POST /vacancies {response.status_code} {response.text[:200]}"
                )
                continue
            vacancy = response.json()
            if not all(r["topic"] for r in vacancy["requirements"]):
                failures.append(f"{item['id']}: требование без темы")
            started = time.perf_counter()
            response = await api.post("/sessions", json={"vacancy_id": vacancy["id"], "n": args.n})
            timings["session"].append(time.perf_counter() - started)
            if response.status_code != 201:
                failures.append(
                    f"{item['id']}: POST /sessions {response.status_code} {response.text[:200]}"
                )
                continue
            questions: list[dict[str, Any]] = response.json()["questions"]
            bad = [
                q
                for q in questions
                if not (q["topic"] and q["requirement_id"] and 3 <= len(q["key_points"]) <= 6)
            ]
            if len(questions) != args.n or bad:
                failures.append(f"{item['id']}: вопросы без темы, требования или key_points")
            print(
                f"  {item['id']}: {vacancy['level']}, {len(vacancy['requirements'])} требований, "
                f"разбор {timings['vacancy'][-1]:.0f} с, вопросы {timings['session'][-1]:.0f} с"
            )

        for item in labels["non_vacancies"]:
            text = (HERE / "non_vacancies" / item["file"]).read_text(encoding="utf-8")
            response = await api.post("/vacancies", json={"user_id": user, "text": text})
            code = response.json().get("error", {}).get("code")
            if (response.status_code, code) != (422, "not_a_vacancy"):
                failures.append(f"{item['file']}: {response.status_code} {code}")
            print(f"  {item['file']}: {response.status_code} {code}")

        # нерабочая ссылка — просьба прислать текст или HTML; страница без вакансии — то же
        # или «не вакансия», если текст со страницы извлёкся и его отклонила модель
        cases = {
            "https://hh.ru/vacancy/1": {"url_unreadable"},
            "https://example.com/": {"url_unreadable", "not_a_vacancy"},
        }
        for url, codes in cases.items():
            response = await api.post("/vacancies", json={"user_id": user, "url": url})
            error = response.json().get("error", {})
            ok = response.status_code == 422 and error.get("code") in codes
            if not ok:
                failures.append(f"{url}: {response.status_code} {error}")
            print(f"  {url}: {response.status_code} {error.get('code')}")

        first = labels["vacancies"][0]["url"]
        async with httpx.AsyncClient(timeout=30) as web:
            html = (await web.get(first, headers={"User-Agent": "Mozilla/5.0"})).text
        response = await api.post("/vacancies", json={"user_id": user, "html": html})
        source = response.json().get("source") if response.status_code == 201 else None
        if source != "html":
            failures.append(f"html: {response.status_code} {response.text[:200]}")
        print(f"  сохранённая страница: {response.status_code} source={source}")

    for name, values in timings.items():
        if values:
            print(f"{name}: медиана {statistics.median(values):.1f} с, макс {max(values):.1f} с")
    print("\nПРОВАЛЫ:" if failures else "\nВсе проверки пройдены.")
    for failure in failures:
        print(" -", failure)


if __name__ == "__main__":
    asyncio.run(main())
